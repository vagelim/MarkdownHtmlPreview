import base64
import html
import http.server
import json
import os
import re
import threading
import webbrowser

import sublime
import sublime_plugin


MARKER = re.compile(r'(?m)^<!-- mhp-comment: ([A-Za-z0-9_-]+) -->\r?\n?')
_server = None


def plugin_unloaded():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


def decode_comment(payload):
    try:
        return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode('utf-8'))
    except (ValueError, UnicodeError, TypeError):
        raise ValueError('Invalid stored comment')


class CommentHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.valid_host():
            self.send_error(403)
            return
        if self.path.startswith('/status/'):
            active = self.path == '/status/' + self.server.token
            self.respond(200 if active else 409, 'active' if active else 'outdated')
            return
        if self.path != '/preview/' + self.server.token:
            self.send_error(403)
            return
        body = self.server.page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

    def do_POST(self):
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        if self.path != '/comment' or not self.valid_host() or self.headers.get('Origin') != origin or self.headers.get('Sec-Fetch-Site') != 'same-origin':
            self.send_error(403)
            return
        length = int(self.headers.get('Content-Length', 0))
        if length < 1 or length > 16384:
            self.send_error(413)
            return
        data = None
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            token = data['token']
            quote = data['quote']
            note = data.get('note', '')
            action = data.get('action', 'add')
            previous = data.get('previous')
            view_id = data['view_id']
            if action not in ('add', 'edit', 'resolve', 'restore') or not isinstance(quote, str) or not isinstance(note, str):
                raise ValueError('Invalid comment')
            if not quote.strip() or (action != 'resolve' and not note.strip()) or len(quote) > 2000 or len(note) > 5000:
                raise ValueError('Comment is empty or too long')
            if action in ('edit', 'resolve', 'restore') and (not isinstance(previous, str) or not re.match(r'^[A-Za-z0-9_-]+$', previous)):
                raise ValueError('Invalid comment marker')
            if not isinstance(view_id, int) or token != self.server.token or view_id != self.server.view_id:
                raise ValueError('Preview is no longer active')
        except (ValueError, KeyError, TypeError):
            outdated = isinstance(data, dict) and data.get('token') != self.server.token
            self.respond(409 if outdated else 400, 'This preview is outdated; reopen it from Sublime' if outdated else 'Invalid comment')
            return
        done = threading.Event()
        result = []

        def insert():
            try:
                view = sublime.active_window().active_view() if sublime.active_window() else None
                if view is None or view.id() != view_id:
                    raise ValueError('Activate the original Markdown tab in Sublime first')
                source = view.substr(sublime.Region(0, view.size()))
                if action in ('add', 'restore'):
                    clean = MARKER.sub('', source)
                    if clean.count(quote) != 1:
                        raise ValueError('Selection is not unique in the Markdown source')
                    payload = base64.urlsafe_b64encode(json.dumps({'quote': quote, 'note': note}, ensure_ascii=False).encode('utf-8')).decode('ascii').rstrip('=')
                    if action == 'restore':
                        old = decode_comment(previous)
                        if old.get('quote') != quote or old.get('note') != note or source.count('<!-- mhp-comment: ' + previous + ' -->'):
                            raise ValueError('Resolved comment has changed; cannot restore')
                        payload = previous
                    view.run_command('insert_markdown_preview_comment', {'payload': payload})
                    view.run_command('save')
                    if view.is_dirty():
                        raise ValueError('Comment was added, but the Markdown file was not saved; save it in Sublime')
                    result.append((200, payload))
                else:
                    marker = '<!-- mhp-comment: ' + previous + ' -->'
                    if source.count(marker) != 1:
                        raise ValueError('Comment changed since this preview opened; reopen the preview')
                    old = decode_comment(previous)
                    if old.get('quote') != quote:
                        raise ValueError('Comment no longer matches the selection')
                    payload = '' if action == 'resolve' else base64.urlsafe_b64encode(json.dumps({'quote': quote, 'note': note}, ensure_ascii=False).encode('utf-8')).decode('ascii').rstrip('=')
                    view.run_command('update_markdown_preview_comment', {'previous': previous, 'payload': payload})
                    view.run_command('save')
                    if view.is_dirty():
                        raise ValueError('Comment was changed, but the Markdown file was not saved; save it in Sublime')
                    result.append((200, payload))
            except ValueError as error:
                result.append((409, str(error)))
            except Exception as error:
                result.append((500, 'Sublime comment operation failed: ' + type(error).__name__))
            finally:
                done.set()

        sublime.set_timeout(insert, 0)
        if not done.wait(10):
            self.respond(503, 'Sublime did not respond')
            return
        self.respond(*result[0])

    def respond(self, status, message):
        body = message.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class InsertMarkdownPreviewCommentCommand(sublime_plugin.TextCommand):
    def run(self, edit, payload):
        text = self.view.substr(sublime.Region(0, self.view.size()))
        separator = '' if not text else ('\n' if text.endswith('\n') else '\n\n')
        self.view.insert(edit, self.view.size(), separator + '<!-- mhp-comment: ' + payload + ' -->\n')


class UpdateMarkdownPreviewCommentCommand(sublime_plugin.TextCommand):
    def run(self, edit, previous, payload):
        text = self.view.substr(sublime.Region(0, self.view.size()))
        match = next((m for m in MARKER.finditer(text) if m.group(1) == previous), None)
        if match is None:
            return
        if payload:
            self.view.replace(edit, sublime.Region(match.start(1), match.end(1)), payload)
        else:
            self.view.erase(edit, sublime.Region(match.start(), match.end()))


class MarkdownCommentPreviewCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.view = self.window.active_view()
        if self.view is None or not self.view.file_name():
            sublime.error_message('Save the Markdown file before opening a comment preview.')
            return
        source = self.view.substr(sublime.Region(0, self.view.size()))
        global _server
        if _server is None:
            _server = http.server.HTTPServer(('127.0.0.1', 0), CommentHandler)
            worker = threading.Thread(target=_server.serve_forever)
            worker.daemon = True
            worker.start()
        server = _server
        server.token = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii').rstrip('=')
        server.view_id = self.view.id()
        template = sublime.load_resource('Packages/Markdown HTML Preview/dist/index.html')
        page = template.replace('%%%CONTENT%%%', html.escape(source, quote=False))
        config = {'port': server.server_port, 'token': server.token, 'view_id': self.view.id()}
        before, closing = page.rsplit('</body>', 1)
        page = before + '<script>window.commentPreviewConfig = ' + json.dumps(config) + ';</script>\n<script>' + SCRIPT + '</script>\n</body>' + closing
        server.page = page
        webbrowser.open('http://127.0.0.1:' + str(server.server_port) + '/preview/' + server.token)


SCRIPT = r'''(function () {
  var pre = document.querySelector('#body pre');
  var raw = pre.textContent;
  var stored = [];
  pre.textContent = raw.replace(/^<!-- mhp-comment: ([A-Za-z0-9_-]+) -->\r?\n?/gm, function (line, encoded) {
    try {
      var bytes = atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4));
      var data = JSON.parse(new TextDecoder().decode(Uint8Array.from(bytes, function (c) { return c.charCodeAt(0); })));
      if (typeof data.quote === 'string' && typeof data.note === 'string') { data.marker = encoded; stored.push(data); }
    } catch (error) {}
    return '';
  });
  window.addEventListener('load', function () {
    var article = document.getElementById('body');
    var style = document.createElement('style');
    style.textContent = 'body{padding-right:66px!important}.comment-selection{position:fixed;z-index:1001;background:#24292e;color:white;border:0;border-radius:6px;padding:8px 12px;box-shadow:0 4px 16px #0003;cursor:pointer;font:13px sans-serif}.comment-selection[hidden]{display:none}.comment-bubble{position:fixed;right:14px;z-index:1000;width:34px;height:34px;border-radius:50%;background:#fff8e3;border:1px solid #c9973e;color:#704f16;box-shadow:0 2px 8px #0002;cursor:pointer;font:600 13px sans-serif}.comment-highlight{position:absolute;pointer-events:none;z-index:1;background:#ffe17d77;border-bottom:2px solid #ca912b}.comment-card{position:fixed;right:56px;z-index:1002;width:min(310px,calc(100vw - 85px));padding:14px;background:#fff;border:1px solid #d0d7de;border-radius:9px;box-shadow:0 8px 28px #0003;font:14px/1.5 sans-serif;color:#24292e}.comment-card blockquote{margin:5px 0 10px;padding-left:9px;border-left:3px solid #e4b64f;color:#57606a;font-size:12px;max-height:70px;overflow:auto}.comment-card textarea{box-sizing:border-box;width:100%;min-height:80px;padding:8px;font:14px sans-serif;border:1px solid #aaa;border-radius:5px}.comment-card button{margin:8px 8px 0 0;padding:6px 10px;cursor:pointer}.comment-card p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.comment-hint{position:fixed;bottom:12px;right:12px;z-index:999;background:#24292ef0;color:white;border-radius:6px;padding:7px 10px;font:12px sans-serif}';
    document.head.appendChild(style);
    var hint = document.createElement('div');
    hint.className = 'comment-hint';
    hint.textContent = 'Select text to comment · changes save automatically · reopen preview to sync';
    document.body.appendChild(hint);
    var toolbar = document.createElement('button');
    toolbar.className = 'comment-selection';
    toolbar.textContent = 'Comment on selection';
    toolbar.hidden = true;
    document.body.appendChild(toolbar);
    var selected = null;
    var notes = [];
    var card = null;
    var marks = [];
    var resolved = [];
    var outdated = false;
    function showOutdated() {
      if (outdated) return;
      outdated = true;
      hint.textContent = 'This preview is outdated; reopen it from Sublime to change comments';
      hint.style.background = '#8a3b16';
      toolbar.hidden = true;
      clearCard();
      document.querySelectorAll('.comment-bubble, .comment-selection').forEach(function (button) { button.disabled = true; });
    }
    function checkStatus() {
      if (outdated) return;
      fetch('/status/' + window.commentPreviewConfig.token, {cache: 'no-store'})
        .then(function (response) { if (response.status === 409) showOutdated(); })
        .catch(function () {});
    }
    function request(action, note, text) {
      if (outdated) return Promise.reject(Error('This preview is outdated; reopen it from Sublime'));
      return fetch('/comment', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: JSON.stringify({token: window.commentPreviewConfig.token, view_id: window.commentPreviewConfig.view_id, action: action, previous: note.marker, quote: note.quote, note: text})})
        .then(function (response) { return response.text().then(function (value) { if (response.status === 409 && value.indexOf('This preview is outdated') === 0) showOutdated(); if (!response.ok) throw Error(value); return value; }); });
    }
    function report(error) { alert(error instanceof TypeError ? 'Cannot reach Sublime from this preview. Reopen Markdown: Preview with Comments in Sublime, then retry in the new tab.' : error.message); }
    checkStatus();
    addEventListener('focus', checkStatus);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) checkStatus(); });
    function refreshNumbers() { notes.forEach(function (note, index) { note.bubble.textContent = index + 1; }); }
    function clearCard() { if (card) card.remove(); card = null; }
    function clearMarks() { marks.forEach(function (el) { el.remove(); }); marks = []; }
    function point(range) {
      var boxes = Array.from(range.getClientRects()).filter(function (r) { return r.width && r.height; });
      return boxes.length ? boxes[boxes.length - 1] : range.getBoundingClientRect();
    }
    function position() {
      clearMarks();
      var bubbleRows = notes.map(function (note) {
        var p = point(note.range);
        return {note: note, desired: Math.max(8, Math.min(innerHeight - 43, p.top + p.height / 2 - 17))};
      }).sort(function (a, b) { return a.desired - b.desired; });
      var spacing = Math.min(38, Math.max(12, (innerHeight - 51) / Math.max(1, bubbleRows.length - 1)));
      var tops = [];
      bubbleRows.forEach(function (row, index) {
        tops.push(Math.max(row.desired, index ? tops[index - 1] + spacing : 8));
      });
      var overflow = tops.length ? Math.max(0, tops[tops.length - 1] - (innerHeight - 43)) : 0;
      bubbleRows.forEach(function (row, index) {
        row.note.bubble.style.top = Math.max(8, tops[index] - overflow) + 'px';
      });
      notes.forEach(function (note) {
        Array.from(note.range.getClientRects()).filter(function (r) { return r.width && r.height; }).forEach(function (r) {
          var mark = document.createElement('div');
          mark.className = 'comment-highlight';
          mark.style.left = (r.left + scrollX) + 'px';
          mark.style.top = (r.top + scrollY) + 'px';
          mark.style.width = r.width + 'px';
          mark.style.height = r.height + 'px';
          document.body.appendChild(mark);
          marks.push(mark);
        });
      });
      if (selected && !toolbar.hidden) {
        var p = point(selected);
        toolbar.style.top = Math.max(8, Math.min(innerHeight - 44, p.bottom + 7)) + 'px';
        toolbar.style.left = Math.max(8, Math.min(innerWidth - 175, p.left)) + 'px';
      }
    }
    function findRange(quote) {
      var text = article.textContent;
      if (text.indexOf(quote) < 0 || text.indexOf(quote, text.indexOf(quote) + 1) >= 0) return null;
      var start = text.indexOf(quote), end = start + quote.length, cursor = 0;
      var walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
      var node, range = document.createRange(), started = false;
      while ((node = walker.nextNode())) {
        var next = cursor + node.length;
        if (!started && start >= cursor && start < next) { range.setStart(node, start - cursor); started = true; }
        if (started && end <= next) { range.setEnd(node, end - cursor); return range; }
        cursor = next;
      }
      return null;
    }
    function showCard(note, editing) {
      clearCard();
      toolbar.hidden = true;
      var p = point(note.range);
      card = document.createElement('div');
      card.className = 'comment-card';
      card.style.top = Math.max(8, Math.min(innerHeight - 245, p.top)) + 'px';
      var title = document.createElement('strong');
      title.textContent = editing ? (note.marker ? 'Edit comment' : 'Add comment') : 'Comment ' + (notes.indexOf(note) + 1);
      card.appendChild(title);
      var quote = document.createElement('blockquote');
      quote.textContent = '“' + note.quote + '”';
      card.appendChild(quote);
      if (editing) {
        var input = document.createElement('textarea');
        input.placeholder = 'Write a comment…';
        input.value = note.text || '';
        card.appendChild(input);
        var save = document.createElement('button');
        save.textContent = note.marker ? 'Save changes' : 'Add comment';
        save.onclick = function () {
          if (!input.value.trim()) return;
          save.disabled = true;
          request(note.marker ? 'edit' : 'add', note, input.value.trim())
            .then(function (marker) {
              note.marker = marker;
              note.text = input.value.trim();
              if (notes.indexOf(note) < 0) addNote(note);
              clearCard();
              selected = null;
              getSelection().removeAllRanges();
              position();
            }).catch(function (error) { save.disabled = false; report(error); });
        };
        card.appendChild(save);
      } else {
        var body = document.createElement('p');
        body.textContent = note.text;
        card.appendChild(body);
        var change = document.createElement('button');
        change.textContent = 'Edit';
        change.onclick = function () { showCard(note, true); };
        card.appendChild(change);
        var resolve = document.createElement('button');
        resolve.textContent = 'Resolve';
        resolve.onclick = function () {
          resolve.disabled = true;
          request('resolve', note, '').then(function () {
            notes.splice(notes.indexOf(note), 1);
            note.bubble.remove();
            resolved.push(note);
            clearCard();
            refreshNumbers();
            position();
            showUndo(note);
          }).catch(function (error) { resolve.disabled = false; report(error); });
        };
        card.appendChild(resolve);
      }
      var close = document.createElement('button');
      close.textContent = 'Close';
      close.onclick = clearCard;
      card.appendChild(close);
      document.body.appendChild(card);
      if (editing) input.focus();
    }
    function showUndo(note) {
      var undo = document.createElement('button');
      undo.className = 'comment-selection';
      undo.textContent = 'Undo resolve';
      undo.style.right = '14px';
      undo.style.bottom = '50px';
      undo.onclick = function () {
        undo.disabled = true;
        request('restore', note, note.text).then(function (marker) {
          note.marker = marker;
          resolved.splice(resolved.indexOf(note), 1);
          addNote(note);
          undo.remove();
        }).catch(function (error) { undo.disabled = false; report(error); });
      };
      document.body.appendChild(undo);
      undo.title = 'Available until this preview is refreshed';
    }
    function addNote(note) {
      note.bubble = document.createElement('button');
      note.bubble.className = 'comment-bubble';
      note.bubble.textContent = notes.length + 1;
      note.bubble.setAttribute('aria-label', 'Open comment on ' + note.quote);
      note.bubble.onclick = function () { showCard(note, false); };
      document.body.appendChild(note.bubble);
      notes.push(note);
      position();
    }
    stored.forEach(function (data) {
      var range = findRange(data.quote);
      if (range) addNote({range: range, quote: data.quote, text: data.note, marker: data.marker});
    });
    document.addEventListener('mouseup', function (event) {
      if (outdated || !article.contains(event.target)) return;
      var selection = getSelection();
      if (!selection || selection.isCollapsed || !selection.toString().trim()) { toolbar.hidden = true; return; }
      var range = selection.getRangeAt(0);
      if (!article.contains(range.commonAncestorContainer)) { toolbar.hidden = true; return; }
      var quote = selection.toString().trim();
      if (!findRange(quote) || range.toString() !== quote) { toolbar.hidden = true; return; }
      selected = range.cloneRange();
      toolbar.hidden = false;
      position();
    });
    toolbar.addEventListener('mousedown', function (event) { event.preventDefault(); });
    toolbar.addEventListener('click', function () {
      if (!outdated && selected) showCard({range: selected.cloneRange(), quote: selected.toString()}, true);
    });
    addEventListener('scroll', position, {passive: true});
    addEventListener('resize', position);
  });
})();'''
