"""Tests of unisa_rag.py from a PC, without ROS or Rasa. Keep this file next to unisa_rag.py.

    python test_unisa_rag.py --offline [--answers FILE.jsonl]
        No Telegram. A fake Telegram Bot API runs on this PC (127.0.0.1) and the real client talks to it over HTTP:
        answer and sources, messages left from earlier questions, messages of other chats, service messages of the
        bot, no answer, wrong token, refused message, several threads, asynchronous use; then the text for the voice.
        With --answers also on real answers: one JSON object per line with the field "risposta" (or "answer"), for
        example results/final_run_2026-10-15/_F.jsonl of the RAG system.

    python test_unisa_rag.py --check
        Verifies the token of the client bot and prints its username.

    python test_unisa_rag.py [--question "..."] [--pause 13] [--out FILE.md]
        Questions to the real bot: the default list below, or one question. Needs the environment variables of
        unisa_rag.py (UNISA_RAG_CLIENT_TOKEN, UNISA_RAG_BOT), "Bot to Bot Communication" on for both bots and the RAG
        bot running without access code. Between two questions the script waits so that at least --pause seconds
        pass from one question to the next: the bot accepts at most 5 messages per minute from a chat. The questions
        are one conversation: the bot keeps its history until it is restarted. The transcript (question, seconds,
        answer of the bot, text for the voice, result) goes to --out, by default test_unisa_rag_<date>_<time>.md in
        the current folder.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import unisa_rag
from unisa_rag import (FALLBACK_ANSWER, EMPTY_QUESTION_ANSWER, UnisaRag, UnisaRagError, check_bot_reply,
                       clean_for_speech, is_answer, is_sources_message)

# questions of the live test, with what to check by eye; 1-6 are those of the demonstration video
QUESTIONS = [
    ("Ciao!",
     "fixed greeting, no sources message, answer within a second"),
    ("Chi è l'attuale Rettore dell'Università di Salerno?",
     "Prof. Virgilio D'Antonio, in office since 1 November 2025"),
    ("Dove si trova l'ufficio della Prof.ssa Alessia Saggese?",
     "Campus di Fisciano, Edificio E, Piano Terzo, Stanza 005, in 1-2 s"),
    ("Quanti uffici fanno capo all'AREA III - Didattica e Ricerca?",
     "13 offices, 4 of them coordination offices"),
    ("In quale anno la Scuola medica salernitana ottenne un primo riconoscimento giuridico e in quale documento?",
     "1231, Costituzioni di Melfi"),
    ("E chi le emanò?",
     "Federico II: the question is completed from the conversation history"),
    ("Qual è l'indirizzo email e il sito web dell'Ufficio Relazioni con il Pubblico dell'Università di Salerno?",
     "urp@unisa.it and web.unisa.it/urp, read aloud as words"),
    ("Chi è Renata Cantilena e a quale Dipartimento afferisce?",
     "DISPAC; e-mail, phone numbers digit by digit, office"),
    ("Quali sono le funzioni e la composizione del Senato Accademico?",
     "long answer with two lists, turned into sentences"),
    ("Mi consigli una pizzeria a Salerno?",
     "off-topic: says it has no information"),
]

# what must not be left in a text for the voice
RESIDUE = re.compile(r"[*`#\[\]|_@\n]|https?://|www\.")

# ---------------------------------------------------------------------------------------------------------------------
# offline tests

SOURCES = ("https://web.unisa.it/ateneo/organi/rettore [Score: 0.99]\n\n"
           "https://rubrica.unisa.it/?matricola=024950 [Score: 0.12]")
WELCOME = "Ciao! Sono l'assistente virtuale dell'Università degli Studi di Salerno.\nCome posso aiutarti oggi?"

CLEANING_CASES = [
    ("L'indirizzo email dell'Ufficio Relazioni con il Pubblico dell'Università degli Studi di Salerno è "
     "**urp@unisa.it**.\n\nIl sito web dedicato alla struttura è https://web.unisa.it/urp.",
     "L'indirizzo email dell'Ufficio Relazioni con il Pubblico dell'Università degli Studi di Salerno è "
     "urp chiocciola unisa punto it. Il sito web dedicato alla struttura è web punto unisa punto it."),
    ("I suoi recapiti sono:\n*   **Email:** rcantilena@unisa.it\n*   **Telefono:** 089 962305 o 089 96 2114\n"
     "*   **Ufficio:** Campus di Fisciano, Edificio B, Piano Secondo, Stanza 071.",
     "I suoi recapiti sono: Email: rcantilena chiocciola unisa punto it. Telefono: 0 8 9 9 6 2 3 0 5 o "
     "0 8 9 9 6 2 1 1 4. Ufficio: Campus di Fisciano, Edificio B, Piano Secondo, Stanza 071."),
    ("*   **Presidio della Qualità:** [www.pqa.unisa.it](http://www.pqa.unisa.it/)\n"
     "*   [Rubrica - Cerca per Nome](https://rubrica.unisa.it#tab-nome)",
     "Presidio della Qualità: pqa punto unisa punto it. Rubrica, Cerca per Nome."),
    ("sono:\n\n1.  **Area I** - Supporto strategico e gestionale\n2.  **Area II** - Risorse",
     "sono: Area I, Supporto strategico e gestionale. Area II, Risorse."),
    ("**Composizione**\nIl Senato è composto da:\n*   Il Rettore, che lo presiede;\n"
     "    *   *Esempio:* Essere membri di una commissione",
     "Composizione. Il Senato è composto da: Il Rettore, che lo presiede; Esempio: Essere membri di una "
     "commissione."),
    ("all'indirizzo `http://www.anvur.it/index.php?option=com_content&view=article&id=475` e la pagina",
     "all'indirizzo anvur punto it e la pagina."),
    ("contatto generale/didattica e/o PEC, Legge 240/2010, Gazzetta Ufficiale n° 176, Fax: +39 089 96 2487",
     "contatto generale o didattica e o PEC, Legge 240/2010, Gazzetta Ufficiale numero 176, Fax: più 3 9 0 8 9 9 "
     "6 2 4 8 7."),
    ("Ufficio Relazioni Internazionali – Erasmus, a.a. 2024–2025, Management & Innovation Systems",
     "Ufficio Relazioni Internazionali, Erasmus, a.a. 2024-2025, Management e Innovation Systems."),
    (WELCOME,
     "Ciao! Sono l'assistente virtuale dell'Università degli Studi di Salerno. Come posso aiutarti oggi?"),
    ("l'e-mail istituzionale con dominio @unisa.it.",
     "l'e-mail istituzionale con dominio chiocciola unisa punto it."),
    ("si trova nel Campus di Fisciano, all'interno dell'Edificio E, al Terzo Piano, nella Stanza 005 (codice "
     "identificativo U.FSTEC-05.P03.005).",
     "si trova nel Campus di Fisciano, all'interno dell'Edificio E, al Terzo Piano, nella Stanza 005."),
    ("Edificio F, Piano Terzo, Stanza 055 (U.FSTEC-07.P03.055) e Stanza 012 (U.FINV-12B.P01.024)",
     "Edificio F, Piano Terzo, Stanza 055 e Stanza 012."),
    ("il Dipartimento di Medicina (codice DIPMED) e il progetto (codice progetto PNC0000001D3 4 Health)",
     "il Dipartimento di Medicina (codice DIPMED) e il progetto (codice progetto PNC0000001D3 4 Health)."),
    ("", ""),
]

TOKEN = "123456:fake-token-for-tests"
CLIENT_ID, RAG_ID, OTHER_ID = 1001, 2002, 3003


class FakeBotApi:
    """A small Telegram Bot API on 127.0.0.1 with getMe, getUpdates (long polling) and sendMessage to @rag_bot.
    Each message sent to @rag_bot gets the replies chosen by `script`, from the RAG bot's chat."""

    def __init__(self, script):
        self.script = script
        self.condition = threading.Condition()
        self.updates = []
        self.last_update = 500
        self.last_message = 100
        self.sent = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                match = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
                length = int(self.headers.get("Content-Length") or 0)
                params = json.loads(self.rfile.read(length) or b"{}")
                status, body = api.handle(match.group(1), match.group(2), params) if match else (404, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def push(self, chat_id, text, message_id=None):
        with self.condition:
            if message_id is None:
                self.last_message += 1
                message_id = self.last_message
            self.last_update += 1
            self.updates.append({"update_id": self.last_update,
                                 "message": {"message_id": message_id, "text": text,
                                             "chat": {"id": chat_id, "type": "private"},
                                             "from": {"id": chat_id, "is_bot": True}}})
            self.condition.notify_all()

    def handle(self, token, method, params):
        if token != TOKEN:
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": CLIENT_ID, "is_bot": True, "username": "client_bot"}}
        if method == "getUpdates":
            offset, wait = params.get("offset", 0), params.get("timeout", 0)
            with self.condition:
                if offset < 0:                      # the last update; the older ones are forgotten
                    self.updates = self.updates[offset:]
                    return 200, {"ok": True, "result": list(self.updates)}
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                end = time.monotonic() + wait
                while not self.updates and time.monotonic() < end:
                    self.condition.wait(end - time.monotonic())
                return 200, {"ok": True, "result": self.updates[:100]}
        if method == "sendMessage":
            if params.get("chat_id") != "@rag_bot":
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            with self.condition:
                self.last_message += 1
                question_id = self.last_message
                self.sent.append(params.get("text"))
            for delay, text, where in self.script(params.get("text")):
                threading.Timer(delay, self._reply, (text, where, question_id)).start()
            return 200, {"ok": True, "result": {"message_id": question_id, "text": params.get("text"),
                                                "chat": {"id": RAG_ID, "type": "private"}}}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def _reply(self, text, where, question_id):
        if where == "other chat":
            self.push(OTHER_ID, text)
        elif where is None:                         # a new message of the RAG bot
            self.push(RAG_ID, text)
        else:                                       # a message older than the question (late sources)
            self.push(RAG_ID, text, message_id=question_id + where)


def bot_script(question):
    """Replies of the fake RAG bot, as (seconds, text, None = new message, negative = older id, or "other chat")."""
    if question == "Ciao!":
        return [(0.05, WELCOME, None)]
    if question == "late sources":
        return [(0.02, SOURCES, -1), (0.1, "Risposta dopo fonti in ritardo.", None), (0.15, SOURCES, None)]
    if question == "other chat":
        return [(0.02, "Messaggio di un'altra chat.", "other chat"), (0.1, "Risposta giusta.", None)]
    if question == "too many":
        return [(0.05, "Hai inviato troppi messaggi in poco tempo: riprova fra 12 secondi.", None)]
    if question == "error":
        return [(0.05, "Mi dispiace, non sono riuscito a rispondere per un errore interno. Riprova fra qualche "
                       "minuto.", None)]
    if question == "code":
        return [(0.05, "Per usare il bot serve il codice di accesso: scrivi /start seguito dal codice, oppure apri "
                       "il collegamento che ti è stato dato.", None)]
    if question == "silence":
        return []
    return [(0.2, "Risposta a: **%s**" % question, None), (0.25, SOURCES, None)]


class Checks:
    def __init__(self):
        self.passed = self.failed = 0

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            print("FAIL  %s %s" % (name, detail))


def raises(function, *args):
    """The message of the UnisaRagError raised by function(*args), or None."""
    try:
        function(*args)
    except UnisaRagError as error:
        return str(error)
    return None


def offline(answers_file):
    c = Checks()

    # messages of the bot
    c.check("sources", is_sources_message(SOURCES))
    c.check("sources without address", is_sources_message("URL Non Disponibile [Score: 0.50]"))
    c.check("answer is not sources", not is_sources_message(CLEANING_CASES[0][0]))
    c.check("empty is not sources", not is_sources_message(""))
    c.check("answer after the question", is_answer(105, "Risposta.", 104))
    c.check("older message skipped", not is_answer(103, "Risposta.", 104))
    c.check("sources skipped", not is_answer(106, SOURCES, 104))
    for text in (WELCOME, "Risposta normale."):
        c.check("reply accepted", raises(check_bot_reply, text) is None)
    for text in ("Hai inviato troppi messaggi in poco tempo: riprova fra 5 secondi.",
                 "Per usare il bot serve il codice di accesso: scrivi /start seguito dal codice.",
                 "Mi dispiace, non sono riuscito a rispondere per un errore interno. Riprova fra qualche minuto.",
                 "  "):
        c.check("service message refused", raises(check_bot_reply, text) is not None, repr(text[:40]))

    # text for the voice
    for raw, expected in CLEANING_CASES:
        got = clean_for_speech(raw)
        c.check("cleaning", got == expected, "\n      got:      %r\n      expected: %r" % (got, expected))

    # the real client against a fake Bot API
    api = FakeBotApi(bot_script)
    try:
        api.push(RAG_ID, "Vecchia risposta arrivata prima del client.")       # must be forgotten
        rag = UnisaRag(bot="rag_bot", token=TOKEN, timeout=2.0, api_url=api.url)
        c.check("username with @ added", rag.bot == "@rag_bot")
        c.check("token not shown", TOKEN not in repr(rag))
        c.check("check", rag.check() == "@client_bot")
        c.check("old messages forgotten", rag.ask("Chi è il Rettore?") == "Risposta a: **Chi è il Rettore?**")
        c.check("query cleans", rag.query("Chi è il Rettore?") == "Risposta a: Chi è il Rettore?")
        c.check("greeting without sources", rag.query("Ciao!") == clean_for_speech(WELCOME))
        c.check("late sources skipped", rag.ask("late sources") == "Risposta dopo fonti in ritardo.")
        c.check("other chats skipped", rag.ask("other chat") == "Risposta giusta.")
        rag.ask("x")  # its sources arrive after the answer and stay in the queue
        time.sleep(0.2)
        c.check("leftovers dropped", rag.ask("y") == "Risposta a: **y**")
        for question in ("too many", "error", "code", "silence"):
            started = time.monotonic()
            c.check("fallback (%s)" % question, rag.query(question) == FALLBACK_ANSWER)
            c.check("ask raises (%s)" % question, raises(rag.ask, question) is not None)
            if question == "silence":
                c.check("timeout respected", time.monotonic() - started < 7.0, "%.1f s" % (time.monotonic() - started))
        sent_before = len(api.sent)
        c.check("empty question", rag.query("   ") == EMPTY_QUESTION_ANSWER and len(api.sent) == sent_before)

        results = {}

        def worker(name):
            results[name] = rag.ask(name)

        threads = [threading.Thread(target=worker, args=("domanda %d" % i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        c.check("several threads", len(results) == 4
                and all(results[name] == "Risposta a: **%s**" % name for name in results))

        async def from_event_loop():
            ticks = 0

            async def ticker():  # the event loop must stay free while the client waits
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            task = asyncio.ensure_future(ticker())
            text = await rag.query_async("asincrona")
            task.cancel()
            return text, ticks

        text, ticks = asyncio.run(from_event_loop())
        c.check("query_async", text == "Risposta a: asincrona.")
        c.check("event loop not blocked", ticks >= 5, "%d ticks" % ticks)
        rag.close()

        wrong_token = UnisaRag(bot="@rag_bot", token="999:wrong", timeout=2.0, api_url=api.url)
        message = raises(wrong_token.check)
        c.check("wrong token", message is not None and "token" in message and "999:wrong" not in message, message)
        c.check("wrong token, fallback", wrong_token.query("domanda") == FALLBACK_ANSWER)
        wrong_bot = UnisaRag(bot="@other_bot", token=TOKEN, timeout=2.0, api_url=api.url)
        message = raises(wrong_bot.ask, "domanda")
        c.check("message refused", message is not None and "Bot to Bot" in message, message)
        message = raises(UnisaRag(bot="", token="", api_url=api.url).check)
        c.check("missing settings", message is not None and "UNISA_RAG_CLIENT_TOKEN" in message, message)
        unreachable = UnisaRag(bot="@rag_bot", token=TOKEN, timeout=2.0, api_url="http://127.0.0.1:9")
        message = raises(unreachable.check)
        c.check("Telegram unreachable", message is not None and "cannot reach" in message, message)
    finally:
        api.stop()

    # real answers
    if answers_file:
        rows = [json.loads(line) for line in open(answers_file, encoding="utf-8") if line.strip()]
        texts = [row.get("risposta", row.get("answer")) for row in rows]
        texts = [t for t in texts if isinstance(t, str)]
        with_residue, missing_words, examples = [], 0, []
        for raw in texts:
            spoken = clean_for_speech(raw)
            if RESIDUE.search(spoken) or not spoken:
                with_residue.append((raw, spoken))
            # every word of the answer must still be there, except those inside addresses and room codes
            plain = unisa_rag._ROOM_CODE.sub("", re.sub(r"\]\([^)]*\)", "]", raw))
            plain = re.sub(r"(?i)(?:https?://|www\.)\S+|\S+@\S+", " ", plain)
            words = set(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", plain))
            lost = [w for w in words if w not in spoken]
            if lost:
                missing_words += 1
                if len(examples) < 3:
                    examples.append(lost[:8])
        c.check("real answers without residue", not with_residue,
                "%d of %d, first: %r" % (len(with_residue), len(texts), with_residue[:1]))
        c.check("real answers keep their words", missing_words == 0,
                "%d of %d lose words, e.g. %r" % (missing_words, len(texts), examples))
        lengths = [len(clean_for_speech(t)) for t in texts]
        print("Real answers: %d; text for the voice: median %d characters, longest %d"
              % (len(texts), statistics.median(lengths), max(lengths)))

    print("Offline tests: %d passed, %d failed" % (c.passed, c.failed))
    return c.failed == 0


# ---------------------------------------------------------------------------------------------------------------------
# live test


def live(questions, pause, out):
    rag = UnisaRag()
    try:
        client = rag.check()
    except UnisaRagError as error:
        print("The client cannot start: %s" % error)
        return False
    print("Client bot %s, RAG bot %s, timeout %g s, at least %g s between questions"
          % (client, rag.bot, rag.timeout, pause))
    records = []
    last_sent = None
    try:
        for number, (question, expected) in enumerate(questions, 1):
            if last_sent is not None:
                time.sleep(max(0.0, pause - (time.monotonic() - last_sent)))
            last_sent = time.monotonic()
            print("\n[%d/%d] %s" % (number, len(questions), question))
            raw, spoken, error = "", "", ""
            try:
                raw = rag.ask(question)
                spoken = clean_for_speech(raw)
            except UnisaRagError as e:
                error, spoken = str(e), FALLBACK_ANSWER
            seconds = time.monotonic() - last_sent
            residue = sorted(set(RESIDUE.findall(spoken))) if not error else []
            result = "ERROR: " + error if error else ("RESIDUE: %s" % " ".join(map(repr, residue)) if residue else "OK")
            print("  %.1f s, %s\n  voice: %s" % (seconds, result, spoken))
            records.append(dict(question=question, expected=expected, seconds=seconds, raw=raw, spoken=spoken,
                                result=result))
    finally:
        rag.close()
        write_transcript(out, rag, records)
    answered = [r for r in records if not r["result"].startswith("ERROR")]
    print("\nAnswered %d of %d; transcript: %s" % (len(answered), len(records), out))
    return len(answered) == len(records) and all(r["result"] == "OK" for r in records)


def write_transcript(path, rag, records):
    lines = ["# Test of unisa_rag.py, %s" % datetime.now().strftime("%Y-%m-%d %H:%M"), "",
             "- Client bot: %s" % rag.client_username, "- RAG bot: %s" % rag.bot,
             "- Timeout: %g s" % rag.timeout, "- unisa_rag.py: %s" % unisa_rag.__file__, ""]
    for number, r in enumerate(records, 1):
        lines += ["## %d. %s" % (number, r["question"]), "",
                  "- Expected: %s" % r["expected"],
                  "- Time: %.1f s" % r["seconds"],
                  "- Result: %s" % r["result"], "",
                  "Answer of the bot (sources dropped):", "", "```text", r["raw"] or "(none)", "```", "",
                  "Text for the voice:", "", "> " + r["spoken"], ""]
    seconds = [r["seconds"] for r in records if not r["result"].startswith("ERROR")]
    if seconds:
        lines += ["## Summary", "",
                  "- Answered: %d of %d" % (len(seconds), len(records)),
                  "- Seconds: median %.1f, longest %.1f" % (statistics.median(seconds), max(seconds)), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Tests of unisa_rag.py.")
    parser.add_argument("--offline", action="store_true", help="tests without Telegram")
    parser.add_argument("--answers", help="with --offline: JSON lines file of real answers")
    parser.add_argument("--check", action="store_true", help="verify the token of the client bot")
    parser.add_argument("--question", help="one question instead of the default list")
    parser.add_argument("--pause", type=float, default=13.0, help="seconds from one question to the next")
    parser.add_argument("--out", help="transcript file of the live test")
    args = parser.parse_args()

    if args.offline:
        sys.exit(0 if offline(args.answers) else 1)
    if args.check:
        try:
            print("Client bot %s, RAG bot %s" % (UnisaRag().check(), UnisaRag().bot))
        except UnisaRagError as error:
            print("The client cannot start: %s" % error)
            sys.exit(1)
        return
    questions = [(args.question, "")] if args.question else QUESTIONS
    out = args.out or "test_unisa_rag_%s.md" % datetime.now().strftime("%Y%m%d_%H%M%S")
    sys.exit(0 if live(questions, args.pause, out) else 1)


if __name__ == "__main__":
    main()
