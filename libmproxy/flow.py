"""
    This module provides more sophisticated flow tracking. These match requests
    with their responses, and provide filtering and interception facilities.
"""
import subprocess, sys, json, hashlib, Cookie, cookielib
import proxy, threading, netstring, filt
import controller, version

class RunException(Exception):
    def __init__(self, msg, returncode, errout):
        Exception.__init__(self, msg)
        self.returncode = returncode
        self.errout = errout


# begin nocover
class RequestReplayThread(threading.Thread):
    def __init__(self, flow, masterq):
        self.flow, self.masterq = flow, masterq
        threading.Thread.__init__(self)

    def run(self):
        try:
            server = proxy.ServerConnection(self.flow.request)
            server.send_request(self.flow.request)
            response = server.read_response()
            response.send(self.masterq)
        except proxy.ProxyError, v:
            err = proxy.Error(self.flow.request, v.msg)
            err.send(self.masterq)
# end nocover


class ClientPlaybackState:
    def __init__(self, flows, exit):
        self.flows, self.exit = flows, exit
        self.current = None

    def count(self):
        return len(self.flows)

    def done(self):
        if len(self.flows) == 0 and not self.current:
            return True
        return False

    def clear(self, flow):
        """
           A request has returned in some way - if this is the one we're
           servicing, go to the next flow.
        """
        if flow is self.current:
            self.current = None

    def tick(self, master, testing=False):
        """
            testing: Disables actual replay for testing.
        """
        if self.flows and not self.current:
            n = self.flows.pop(0)
            n.request.client_conn = None
            self.current = master.handle_request(n.request)
            if not testing and not self.current.response:
                #begin nocover
                master.replay_request(self.current)
                #end nocover
            elif self.current.response:
                master.handle_response(self.current.response)


class ServerPlaybackState:
    def __init__(self, headers, flows, exit):
        """
            headers: A case-insensitive list of request headers that should be
            included in request-response matching.
        """
        self.headers, self.exit = headers, exit
        self.fmap = {}
        for i in flows:
            if i.response:
                l = self.fmap.setdefault(self._hash(i), [])
                l.append(i)

    def count(self):
        return sum([len(i) for i in self.fmap.values()])

    def _hash(self, flow):
        """
            Calculates a loose hash of the flow request.
        """
        r = flow.request
        key = [
            str(r.host),
            str(r.port),
            str(r.scheme),
            str(r.method),
            str(r.path),
            str(r.content),
        ]
        if self.headers:
            hdrs = []
            for i in self.headers:
                v = r.headers[i]
                # Slightly subtle: we need to convert everything to strings
                # to prevent a mismatch between unicode/non-unicode.
                v = [str(x) for x in v]
                hdrs.append((i, v))
            key.append(repr(hdrs))
        return hashlib.sha256(repr(key)).digest()

    def next_flow(self, request):
        """
            Returns the next flow object, or None if no matching flow was
            found.
        """
        l = self.fmap.get(self._hash(request))
        if not l:
            return None
        return l.pop(0)


class StickyCookieState:
    def __init__(self, flt):
        """
            flt: A compiled filter.
        """
        self.jar = {}
        self.flt = flt

    def ckey(self, m, f):
        """
            Returns a (domain, port, path) tuple.
        """
        return (
            m["domain"] or f.request.host,
            f.request.port,
            m["path"] or "/"
        )

    def handle_response(self, f):
        for i in f.response.headers["set-cookie"]:
            # FIXME: We now know that Cookie.py screws up some cookies with
            # valid RFC 822/1123 datetime specifications for expiry. Sigh.
            c = Cookie.SimpleCookie(str(i))
            m = c.values()[0]
            k = self.ckey(m, f)
            if cookielib.domain_match(f.request.host, k[0]):
                self.jar[self.ckey(m, f)] = m

    def handle_request(self, f):
        if f.match(self.flt):
            for i in self.jar.keys():
                match = [
                    cookielib.domain_match(i[0], f.request.host),
                    f.request.port == i[1],
                    f.request.path.startswith(i[2])
                ]
                if all(match):
                    l = f.request.headers["cookie"]
                    f.request.stickycookie = True
                    l.append(self.jar[i].output(header="").strip())
                    f.request.headers["cookie"] = l


class StickyAuthState:
    def __init__(self, flt):
        """
            flt: A compiled filter.
        """
        self.flt = flt
        self.hosts = {}

    def handle_request(self, f):
        if "authorization" in f.request.headers:
            self.hosts[f.request.host] = f.request.headers["authorization"]
        elif f.match(self.flt):
            if f.request.host in self.hosts:
                f.request.headers["authorization"] = self.hosts[f.request.host]


class Flow:
    def __init__(self, request):
        self.request = request
        self.response, self.error = None, None
        self.intercepting = False
        self._backup = None

    @classmethod
    def from_state(klass, state):
        f = klass(None)
        f.load_state(state)
        return f

    def script_serialize(self):
        data = self.get_state()
        return json.dumps(data)

    @classmethod
    def script_deserialize(klass, data):
        try:
            data = json.loads(data)
        except Exception:
            return None
        return klass.from_state(data)

    def run_script(self, path):
        """
            Run a script on a flow.

            Returns a (flow, stderr output) tuple, or raises RunException if
            there's an error.
        """
        self.backup()
        data = self.script_serialize()
        try:
            p = subprocess.Popen(
                    [path],
                    stdout=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except OSError, e:
            raise RunException(e.args[1], None, None)
        so, se = p.communicate(data)
        if p.returncode:
            raise RunException(
                "Script returned error code %s"%p.returncode,
                p.returncode,
                se
            )
        f = Flow.script_deserialize(so)
        if not f:
            raise RunException(
                    "Invalid response from script.",
                    p.returncode,
                    se
                )
        self.load_state(f.get_state())
        return se

    def get_state(self, nobackup=False):
        d = dict(
            request = self.request.get_state() if self.request else None,
            response = self.response.get_state() if self.response else None,
            error = self.error.get_state() if self.error else None,
            version = version.IVERSION
        )
        if nobackup:
            d["backup"] = None
        else:
            d["backup"] = self._backup
        return d

    def load_state(self, state):
        self._backup = state["backup"]
        if self.request:
            self.request.load_state(state["request"])
        else:
            self.request = proxy.Request.from_state(state["request"])

        if state["response"]:
            if self.response:
                self.response.load_state(state["response"])
            else:
                self.response = proxy.Response.from_state(self.request, state["response"])
        else:
            self.response = None

        if state["error"]:
            if self.error:
                self.error.load_state(state["error"])
            else:
                self.error = proxy.Error.from_state(state["error"])
        else:
            self.error = None

    def modified(self):
        # FIXME: Save a serialization in backup, compare current with
        # backup to detect if flow has _really_ been modified.
        if self._backup:
            return True
        else:
            return False

    def backup(self):
        self._backup = self.get_state(nobackup=True)

    def revert(self):
        if self._backup:
            self.load_state(self._backup)
            self._backup = None

    def match(self, pattern):
        if pattern:
            if self.response:
                return pattern(self.response)
            elif self.request:
                return pattern(self.request)
        else:
            return True

    def kill(self, master):
        self.error = proxy.Error(self.request, "Connection killed")
        if self.request and not self.request.acked:
            self.request.ack(None)
        elif self.response and not self.response.acked:
            self.response.ack(None)
        master.handle_error(self.error)
        self.intercepting = False

    def intercept(self):
        self.intercepting = True

    def accept_intercept(self):
        if self.request:
            if not self.request.acked:
                self.request.ack()
            elif self.response and not self.response.acked:
                self.response.ack()
            self.intercepting = False

    def replace(self, pattern, repl, *args, **kwargs):
        """
            Replaces a regular expression pattern with repl in all parts of the
            flow . Returns the number of replacements made.
        """
        c = self.request.replace(pattern, repl, *args, **kwargs)
        if self.response:
            c += self.response.replace(pattern, repl, *args, **kwargs)
        if self.error:
            c += self.error.replace(pattern, repl, *args, **kwargs)
        return c


class State(object):
    def __init__(self):
        self._flow_map = {}
        self._flow_list = []
        self.view = []

        # These are compiled filt expressions:
        self._limit = None
        self.intercept = None
        self._limit_txt = None

    @property
    def limit_txt(self):
        return self._limit_txt

    def flow_count(self):
        return len(self._flow_map)

    def active_flow_count(self):
        c = 0
        for i in self._flow_list:
            if not i.response and not i.error:
                c += 1
        return c

    def add_request(self, req):
        """
            Add a request to the state. Returns the matching flow.
        """
        f = Flow(req)
        self._flow_list.append(f)
        self._flow_map[req] = f
        if f.match(self._limit):
            self.view.append(f)
        return f

    def add_response(self, resp):
        """
            Add a response to the state. Returns the matching flow.
        """
        f = self._flow_map.get(resp.request)
        if not f:
            return False
        f.response = resp
        if f.match(self._limit) and not f in self.view:
            self.view.append(f)
        return f

    def add_error(self, err):
        """
            Add an error response to the state. Returns the matching flow, or
            None if there isn't one.
        """
        if err.request:
            f = self._flow_map.get(err.request)
            if not f:
                return None
        else:
            return None
        f.error = err
        if f.match(self._limit) and not f in self.view:
            self.view.append(f)
        return f

    def load_flows(self, flows):
        self._flow_list.extend(flows)
        for i in flows:
            self._flow_map[i.request] = i
        self.recalculate_view()

    def set_limit(self, txt):
        if txt:
            f = filt.parse(txt)
            if not f:
                return "Invalid filter expression."
            self._limit = f
            self._limit_txt = txt
        else:
            self._limit = None
            self._limit_txt = None
        self.recalculate_view()

    def set_intercept(self, txt):
        if txt:
            f = filt.parse(txt)
            if not f:
                return "Invalid filter expression."
            self.intercept = f
            self.intercept_txt = txt
        else:
            self.intercept = None
            self.intercept_txt = None

    def recalculate_view(self):
        if self._limit:
            self.view = [i for i in self._flow_list if i.match(self._limit)]
        else:
            self.view = self._flow_list[:]

    def delete_flow(self, f):
        if f.request in self._flow_map:
            del self._flow_map[f.request]
        self._flow_list.remove(f)
        if f.match(self._limit):
            self.view.remove(f)
        return True

    def clear(self):
        for i in self._flow_list[:]:
            self.delete_flow(i)

    def accept_all(self):
        for i in self._flow_list[:]:
            i.accept_intercept()

    def revert(self, f):
        f.revert()

    def killall(self, master):
        for i in self._flow_list:
            i.kill(master)



class FlowMaster(controller.Master):
    def __init__(self, server, state):
        controller.Master.__init__(self, server)
        self.state = state
        self.server_playback = None
        self.client_playback = None
        self.kill_nonreplay = False
        self.plugin = None

        self.stickycookie_state = False
        self.stickycookie_txt = None

        self.stickyauth_state = False
        self.stickyauth_txt = None

        self.anticache = False
        self.anticomp = False
        self.autodecode = False
        self.refresh_server_playback = False

    def _runscript(self, f, script):
        #begin nocover
        raise NotImplementedError
        #end nocover

    def add_event(self, e, level="info"):
        """
            level: info, error
        """
        pass

    def set_plugin(self, p):
        self.plugin = p

    def set_stickycookie(self, txt):
        if txt:
            flt = filt.parse(txt)
            if not flt:
                return "Invalid filter expression."
            self.stickycookie_state = StickyCookieState(flt)
            self.stickycookie_txt = txt
        else:
            self.stickycookie_state = None
            self.stickycookie_txt = None

    def set_stickyauth(self, txt):
        if txt:
            flt = filt.parse(txt)
            if not flt:
                return "Invalid filter expression."
            self.stickyauth_state = StickyAuthState(flt)
            self.stickyauth_txt = txt
        else:
            self.stickyauth_state = None
            self.stickyauth_txt = None

    def start_client_playback(self, flows, exit):
        """
            flows: A list of flows.
        """
        self.client_playback = ClientPlaybackState(flows, exit)

    def stop_client_playback(self):
        self.client_playback = None

    def start_server_playback(self, flows, kill, headers, exit):
        """
            flows: A list of flows.
            kill: Boolean, should we kill requests not part of the replay?
        """
        self.server_playback = ServerPlaybackState(headers, flows, exit)
        self.kill_nonreplay = kill

    def stop_server_playback(self):
        self.server_playback = None

    def do_server_playback(self, flow):
        """
            This method should be called by child classes in the handle_request
            handler. Returns True if playback has taken place, None if not.
        """
        if self.server_playback:
            rflow = self.server_playback.next_flow(flow)
            if not rflow:
                return None
            response = proxy.Response.from_state(flow.request, rflow.response.get_state())
            response.set_replay()
            flow.response = response
            if self.refresh_server_playback:
                response.refresh()
            flow.request.ack(response)
            return True
        return None

    def tick(self, q):
        if self.client_playback:
            e = [
                self.client_playback.done(),
                self.client_playback.exit,
                self.state.active_flow_count() == 0
            ]
            if all(e):
                self.shutdown()
            self.client_playback.tick(self)

        if self.server_playback:
            if self.server_playback.exit and self.server_playback.count() == 0:
                self.shutdown()

        return controller.Master.tick(self, q)

    def load_flows(self, fr):
        """
            Load flows from a FlowReader object.
        """
        for i in fr.stream():
            if i.request:
                self.handle_request(i.request)
            if i.response:
                self.handle_response(i.response)
            if i.error:
                self.handle_error(i.error)

    def process_new_request(self, f):
        if self.stickycookie_state:
            self.stickycookie_state.handle_request(f)
        if self.stickyauth_state:
            self.stickyauth_state.handle_request(f)

        if self.anticache:
            f.request.anticache()
        if self.anticomp:
            f.request.anticomp()

        if self.server_playback:
            pb = self.do_server_playback(f)
            if not pb:
                if self.kill_nonreplay:
                    f.kill(self)
                else:
                    f.request.ack()

    def process_new_response(self, f):
        if self.stickycookie_state:
            self.stickycookie_state.handle_response(f)

    def replay_request(self, f):
        """
            Returns None if successful, or error message if not.
        """
        #begin nocover
        if f.intercepting:
            return "Can't replay while intercepting..."
        if f.request:
            f.request.set_replay()
            if f.request.content:
                f.request.headers["content-length"] = [str(len(f.request.content))]
            f.response = None
            f.error = None
            self.process_new_request(f)
            rt = RequestReplayThread(f, self.masterq)
            rt.start()
        #end nocover

    def handle_clientconnect(self, r):
        self.add_event("Connect from: %s:%s"%r.address)
        r.ack()

    def handle_clientdisconnect(self, r):
        s = "Disconnect from: %s:%s"%r.client_conn.address
        self.add_event(s)
        if r.client_conn.requestcount:
            s = "    -> handled %s requests"%r.client_conn.requestcount
            self.add_event(s)
        if r.client_conn.connection_error:
            self.add_event(
                "   -> error: %s"%r.client_conn.connection_error, "error"
            )
        r.ack()

    def handle_error(self, r):
        f = self.state.add_error(r)
        if self.client_playback:
            self.client_playback.clear(f)
        r.ack()
        return f

    def handle_request(self, r):
        f = self.state.add_request(r)
        self.process_new_request(f)
        return f

    def handle_response(self, r):
        f = self.state.add_response(r)
        if self.client_playback:
            self.client_playback.clear(f)
        if not f:
            r.ack()
        self.process_new_response(f)
        return f


class FlowWriter:
    def __init__(self, fo):
        self.fo = fo
        self.ns = netstring.FileEncoder(fo)

    def add(self, flow):
        d = flow.get_state()
        s = json.dumps(d)
        self.ns.write(s)


class FlowReadError(Exception):
    @property
    def strerror(self):
        return self.args[0]


class FlowReader:
    def __init__(self, fo):
        self.fo = fo
        self.ns = netstring.decode_file(fo)

    def stream(self):
        """
            Yields Flow objects from the dump.
        """
        try:
            for i in self.ns:
                data = json.loads(i)
                yield Flow.from_state(data)
        except netstring.DecoderError:
            raise FlowReadError("Invalid data format.")

