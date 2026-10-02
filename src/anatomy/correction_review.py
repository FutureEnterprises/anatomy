"""Explicit local human review. Excerpts stay in RAM and the loopback browser.

The user starts this command to view private excerpts. Only opaque keys and
human-selected enums are persisted. This is not an automated gold labeler.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import coach, label_audit
from .ingest import claude
from .privacy import PrivacyError


HTML = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anatomy · Check the labels</title>
<style>
body{font:16px/1.55 system-ui,sans-serif;margin:0;background:#f6f5f1;color:#252722}
main{max-width:820px;margin:42px auto;padding:0 24px}h1{font-size:32px;margin:4px 0}
.muted{color:#60665c;font-size:14px}.card{background:white;border:1px solid #dedfd8;border-radius:12px;padding:22px;margin:22px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.6 system-ui,sans-serif;max-height:360px;overflow:auto}
button{display:block;width:100%;text-align:left;background:#fff;border:1px solid #c9cec3;border-radius:8px;padding:12px 14px;margin:9px 0;color:inherit;font:inherit;cursor:pointer}
button:hover{background:#eaf0e4;border-color:#7f956e}button:disabled{opacity:.5;cursor:default}
button small{display:block;font-size:13px;color:#60665c}#message{min-height:24px;color:#43583a}
</style><main><div class="muted">ANATOMY · LOCAL HUMAN REVIEW</div><h1>Check the labels</h1>
<p>Judge what the user is saying. Machine labels stay hidden so they don't influence your review.</p>
<p class="muted">These excerpts stay in this local browser. Saved answers contain only an opaque key and your chosen label. When an excerpt lacks enough context, choose “Unclear.”</p>
<div id="progress" class="muted"></div><section id="item">
<div class="card"><div class="muted">Previous assistant · last 500 characters</div><pre id="assistant"></pre>
<div class="muted">User message · first 1,500 characters</div><pre id="user"></pre></div>
<div id="choices"></div></section><div id="message" role="status"></div>
<script>
const labels=[['correction','Corrects the agent','Says its recent work was wrong, broken, incomplete or ignored an instruction.'],
['bug_report','Reports a system failure','Describes a failure without saying the agent’s recent work caused it.'],
['retry','Retries after a failure','A bare retry after an outage or tool failure.'],
['nudge','Checks progress','A status poke without saying anything is wrong.'],
['new','Moves the work forward','A new request, new information, changed requirement or other normal iteration.'],
['unknown','Unclear','The excerpt does not provide enough information to decide.']];
let current=null, busy=false;
const el=id=>document.getElementById(id), endpoint=location.pathname+'item';
function show(data){current=data;el('progress').textContent=data.reviewed+' of '+data.total+' reviewed';
 el('item').hidden=data.done; if(data.done){el('message').textContent='Review saved. Run correction-audit to compare these answers with the machine labels.';return;}
 el('assistant').textContent=data.previous_assistant||'(No preceding text available)';el('user').textContent=data.user;
 el('choices').replaceChildren();for(const [label,title,description] of labels){let b=document.createElement('button');b.textContent=title;
 let s=document.createElement('small');s.textContent=description;b.append(s);b.onclick=()=>answer(label);el('choices').append(b);}}
async function answer(label){if(busy||!current)return;busy=true;for(const b of document.querySelectorAll('button'))b.disabled=true;
 try{const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:current.position,label})});
 if(!r.ok)throw Error();show(await r.json());el('message').textContent=current.done?'Review saved. Run correction-audit to compare these answers with the machine labels.':'Answer saved.';
 }catch{el('message').textContent='The answer could not be saved. Stop the review server and check the saved file before continuing.';}finally{busy=false;for(const b of document.querySelectorAll('button'))b.disabled=false;}}
fetch(endpoint,{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(show).catch(()=>el('message').textContent='The local review server is unavailable.');
</script></main></html>'''


class ReviewState:
    def __init__(self, items, output):
        self.items = items
        self.output = output
        self.answers = []
        self.failed = False

    def current(self):
        position = len(self.answers)
        data = {'reviewed': position, 'total': len(self.items), 'done': position == len(self.items)}
        if not data['done']:
            item = self.items[position]
            data.update(position=position, user=item['text'], previous_assistant=item['tail'])
        return data

    def answer(self, payload):
        if self.failed:
            raise OSError('review storage unavailable')
        if (not isinstance(payload, dict) or set(payload) != {'position', 'label'} or
                type(payload['position']) is not int or payload['position'] != len(self.answers) or
                len(self.answers) >= len(self.items) or
                payload['label'] not in coach.LABELS + (coach.UNKNOWN,)):
            return False
        row = {'key': self.items[len(self.answers)]['key'], 'label': payload['label']}
        try:
            self.output.write(json.dumps(row, allow_nan=False) + '\n')
            self.output.flush()
            os.fsync(self.output.fileno())
        except (OSError, ValueError):
            # The append may have partially persisted. Never accept a retry
            # that could put a conflicting second label behind it.
            self.failed = True
            raise OSError('review storage unavailable') from None
        self.answers.append(row)
        return True


def make_server(items, output, port=0):
    state = ReviewState(items, output)
    token = secrets.token_urlsafe(32)
    prefix = '/' + token + '/'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:%d' % self.server.server_port

        def _reply(self, status, body=b'', kind='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._valid_host():
                return self._reply(403)
            if self.path == prefix:
                return self._reply(200, HTML.encode(), 'text/html; charset=utf-8')
            if self.path == prefix + 'item':
                if state.failed:
                    return self._reply(500)
                return self._reply(200, json.dumps(state.current(), ensure_ascii=True).encode())
            self._reply(404)

        def do_POST(self):
            origin = 'http://127.0.0.1:%d' % self.server.server_port
            if (not self._valid_host() or self.path != prefix + 'item' or
                    self.headers.get('Origin') != origin or
                    self.headers.get('Content-Type') != 'application/json'):
                return self._reply(403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    return self._reply(400)
                payload = json.loads(self.rfile.read(length))
                if not state.answer(payload):
                    return self._reply(400)
            except (ValueError, TypeError):
                return self._reply(400)
            except OSError:
                return self._reply(500)
            self._reply(200, json.dumps(state.current(), ensure_ascii=True).encode())

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.timeout = 1
    return server, state, 'http://127.0.0.1:%d%s' % (server.server_port, prefix)


def main(args):
    created_output = created_manifest = ready = False
    manifest_path = args.out + '.sample.json'
    try:
        if args.sample < 1 or not 0 <= args.port <= 65535:
            raise coach.Refusal('sample size must be positive and port must be between 0 and 65535')
        if os.path.exists(args.out):
            raise coach.Refusal('review output already exists; use a new file')
        if os.path.exists(manifest_path):
            raise coach.Refusal('review sample manifest already exists; use a new file')
        from .cli import _parse_until
        try:
            until, _ = _parse_until(args.until)
        except ValueError:
            raise coach.Refusal('--until must be an ISO time') from None
        root = os.path.expanduser(args.claude_dir or claude.default_root())
        sessions, errors = coach.collect(root, until=until, workers=1)
        prompts = coach.unique_prompts(sessions)
        manifest = label_audit.sample([p['key'] for p in prompts], args.sample, args.seed)
        # The manifest is bound to the cohort, not to machine predictions. The
        # frontend never receives machine labels, raw source paths or ids.
        lookup = {p['key']: p for p in prompts}
        selected = [lookup[row['key']] for row in manifest['sample']]
        encoded_manifest = label_audit.export_sample(manifest)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_output = True
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            server, state, url = make_server(selected, output, args.port)
            with server:
                manifest_fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created_manifest = True
                with os.fdopen(manifest_fd, 'w', encoding='utf-8') as manifest_output:
                    manifest_output.write(encoded_manifest)
                ready = True
                sys.stdout.write(json.dumps({'status': 'local-review-ready', 'url': url,
                    'sample_size': len(selected), 'read_errors': sum(errors.values())}) + '\n')
                sys.stdout.flush()
                server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except coach.Refusal as error:
        sys.stderr.write('anatomy: %s\n' % error)
        return 2
    except (OSError, PrivacyError) as error:
        sys.stderr.write('anatomy: local review unavailable (%s)\n' % type(error).__name__)
        return 2
    except Exception as error:
        sys.stderr.write('anatomy: local review failed (%s)\n' % type(error).__name__)
        return 1
    finally:
        if not ready:
            for path, created in ((args.out, created_output), (manifest_path, created_manifest)):
                if created:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
    return 0
