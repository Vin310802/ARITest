"""UnisaRag: Telegram client of the RAG assistant of the University of Salerno, for the robot's Rasa actions.

It takes the text of a question, sends it to the Telegram bot of the RAG system, waits for the answer, drops the
message with the sources and cleans the answer for speech synthesis: no markdown, lists turned into sentences, web
addresses, e-mail addresses and phone numbers written the way they are read aloud. It returns that text.
It does not depend on ROS or Rasa: a Rasa action imports it, a test script can use it alone.

The client is itself a Telegram bot: it talks to the RAG bot through the Telegram Bot API (bot-to-bot communication,
Bot API 10.0), so it needs only a bot token: no phone number, no login code, no session file.
Requirements: Python 3.8 or later, nothing else (only the standard library), and access to https://api.telegram.org.

Set-up, once:
 1. In Telegram, open @BotFather and create the client bot with /newbot: it gives the token of the new bot.
 2. In @BotFather turn on "Bot to Bot Communication" for BOTH bots, the client bot and the RAG bot (bot settings,
    in the BotFather mini app; in some versions also /mybots -> the bot -> Bot Settings).
 3. Set the environment variables:
        UNISA_RAG_CLIENT_TOKEN         123456789:AA...        token of the client bot (step 1); keep it private
        UNISA_RAG_BOT                  @AteneoUnisaBot        username of the RAG bot, for example @name_of_the_bot
        UNISA_RAG_TIMEOUT      (optional)      seconds to wait for an answer, default 90

Use:
    from unisa_rag import UnisaRag        # in rasa_ws/actions: from .unisa_rag import UnisaRag
    rag = UnisaRag()                      # one instance for the whole process
    text = rag.query("Chi è l'attuale Rettore dell'Università di Salerno?")              # synchronous code
    text = await rag.query_async("Chi è l'attuale Rettore dell'Università di Salerno?")  # async def run(...)

query() and query_async() always return a text to speak: if the bot does not answer, or answers with one of its
service messages (too many messages, internal error...), they return FALLBACK_ANSWER and log the reason.
ask() and ask_async() return the answer as the bot wrote it and raise UnisaRagError instead.
check() verifies the token and returns the username of the client bot.
The log never contains the text of questions and answers, only their length and the time taken, nor the token.

Notes:
 - only one program at a time can read the messages of the client bot (Telegram refuses a second reader): do not run
   two programs with the same token, for example the test on a PC and the Rasa action server;
 - the RAG bot answers with two messages: the answer, then the list of sources (lines "address [Score: 0.99]"), which
   is dropped;
 - it accepts at most 5 messages per minute from each chat (BOT_RATE_LIMIT, BOT_RATE_WINDOW of telegram_bot.py);
 - it keeps the conversation history of each chat in memory: all the questions sent by this client are one
   conversation, until the RAG bot is restarted;
 - it answers one message at a time: if an answer arrives after the timeout, it can be taken as the answer to the
   next question, so the timeout must be longer than the slowest answers (40 s measured, 90 s default).
"""

import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("unisa_rag")

# texts for the voice when there is no answer
FALLBACK_ANSWER = "Mi dispiace, in questo momento non riesco a rispondere. Riprova tra poco."
EMPTY_QUESTION_ANSWER = "Non ho capito la domanda. Puoi ripeterla?"

DEFAULT_TIMEOUT = 90.0
DEFAULT_API_URL = "https://api.telegram.org"
POLL_SECONDS = 25          # long polling of getUpdates
HTTP_MARGIN = 15.0         # seconds allowed to each HTTP request on top of its long polling

# service messages of the bot (beginning of the text, as in telegram_bot.py)
BOT_TOO_MANY = "Hai inviato troppi messaggi in poco tempo"
BOT_ASK_CODE = "Per usare il bot serve il codice di accesso"
BOT_INTERNAL_ERROR = "Mi dispiace, non sono riuscito a rispondere per un errore interno"

# one line of the sources message: "<address> [Score: 0.99]"
SOURCES_LINE = re.compile(r".+ \[Score: -?\d+(?:\.\d+)?\]")


class UnisaRagError(Exception):
    """The bot gave no answer to speak."""


# ---------------------------------------------------------------------------------------------------------------------
# messages of the bot


def is_sources_message(text: str) -> bool:
    """True for the second message of the bot, the list of sources."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(SOURCES_LINE.fullmatch(line) for line in lines)


def is_answer(message_id: int, text: str, question_id: int) -> bool:
    """True for a message of the bot that answers the question with id question_id: it comes after the question and
    it is not a list of sources. Older messages (sources or late answers to earlier questions) are skipped."""
    return message_id > question_id and not is_sources_message(text)


def check_bot_reply(text: str) -> None:
    """Raise UnisaRagError if the reply of the bot is one of its service messages instead of an answer."""
    reply = text.strip()
    if not reply:
        raise UnisaRagError("empty reply from the bot")
    if reply.startswith(BOT_TOO_MANY):
        raise UnisaRagError("the bot refused the question: more than 5 messages in a minute from this chat")
    if reply.startswith(BOT_ASK_CODE):
        raise UnisaRagError("the bot asks for an access code: start it without BOT_ACCESS_CODE")
    if reply.startswith(BOT_INTERNAL_ERROR):
        raise UnisaRagError("internal error of the bot (details in its log)")


# ---------------------------------------------------------------------------------------------------------------------
# text for the voice

# Italian words used to read symbols aloud
SAY_DOT = " punto "
SAY_AT = " chiocciola "
SAY_HYPHEN = " trattino "
SAY_UNDERSCORE = " trattino basso "
SAY_PLUS = "più "
SAY_NUMBER = "numero "
SAY_OR = " o "
SAY_AND = " e "

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^()\s]+)\)")              # [label](address)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\[\]\"'`]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")  # also "dominio @unisa.it"
_PHONE = re.compile(r"(?<![\w+])(?:\+39[ .]?)?0\d(?:[ .]?\d){6,9}(?!\w)")  # Italian numbers: 0 and 8-11 digits
# room codes in brackets, "(U.FSTEC-05.P03.005)" or "(codice identificativo U.FINV-12B.P01.024)": not useful by voice
_ROOM_CODE = re.compile(r"\s*\(\s*(?i:codice(?:\s+identificativo)?\s*:?\s*)?"
                        r"[A-Z]\.[A-Z]{2,}[A-Z0-9]*-[A-Z0-9]+(?:\.[A-Z0-9]+)+\s*\)")
_LIST_MARK = re.compile(r"^\s*(?:[*\-•+]|\d{1,3}[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_LETTER = "A-Za-zÀ-ÖØ-öø-ÿ"
_END_PUNCTUATION = ".!?:;,…"


def _say_dotted(text: str) -> str:
    return (text.replace(".", SAY_DOT).replace("_", SAY_UNDERSCORE).replace("-", SAY_HYPHEN)
            .replace("@", SAY_AT))


def _say_url(match) -> str:
    """Read an address as its site only: https://web.unisa.it/urp -> web punto unisa punto it."""
    url, trail = match.group(0), ""
    while url and url[-1] in ".,;:!?":
        url, trail = url[:-1], url[-1] + trail
    site = re.sub(r"(?i)^(?:https?://)?(?:www\.)?", "", url)
    site = re.split(r"[/?#:]", site, maxsplit=1)[0]
    return _say_dotted(site) + trail


def _say_email(match) -> str:
    local, domain = match.group(0).split("@", 1)
    return (_say_dotted(local) + SAY_AT if local else SAY_AT.lstrip()) + _say_dotted(domain)


def _say_phone(match) -> str:
    """Read a phone number digit by digit: 089 962305 -> 0 8 9 9 6 2 3 0 5."""
    number = match.group(0)
    prefix = SAY_PLUS if number.startswith("+") else ""
    return prefix + " ".join(ch for ch in number if ch.isdigit())


def clean_for_speech(text: str) -> str:
    """Turn an answer of the bot into plain text for speech synthesis.

    Markdown marks (bold, italics, headings, code) are removed; each line of a list becomes a sentence; links keep
    their label; web addresses are read as their site; e-mail addresses with "punto" and "chiocciola"; phone numbers
    digit by digit; room codes in brackets are dropped; dashes, slashes and "&" become words or pauses; everything
    ends on one line.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _ROOM_CODE.sub("", text)

    sentences = []
    for line in text.split("\n"):
        line = _HEADING.sub("", line)
        line = _LIST_MARK.sub("", line)
        line = line.replace("*", "").replace("`", "").replace("__", "").strip()
        if not line:
            continue
        if line[-1] not in _END_PUNCTUATION:
            line += "."
        sentences.append(line)
    text = " ".join(sentences)

    text = _URL.sub(_say_url, text)
    text = _EMAIL.sub(_say_email, text)
    text = _PHONE.sub(_say_phone, text)

    text = re.sub(r"\s+[–—-]\s+", ", ", text)        # "Area I - Supporto" -> "Area I, Supporto"
    text = re.sub(r"[–—]", "-", text)                 # "2024–2025" -> "2024-2025"
    text = re.sub(r"\s+/\s+", ", ", text)
    text = re.sub(r"\be/o\b", "e o", text)
    text = re.sub(r"(?<=[%s])/(?=[%s])" % (_LETTER, _LETTER), SAY_OR, text)  # "generale/didattica"
    text = text.replace("&", SAY_AND)
    text = re.sub(r"\b[nN]°\s*", SAY_NUMBER, text)
    text = re.sub(r"[\"“”«»\[\]#|]", "", text)
    text = text.replace("_", " ")

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:!?)])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"[,;:]\.", ".", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip()


# ---------------------------------------------------------------------------------------------------------------------
# client


class UnisaRag:
    """Client of the RAG bot. One instance per process; it can be used from several threads, one question at a time.

    The client bot reads its messages with getUpdates (long polling), so it needs no public address or webhook.
    """

    def __init__(self, bot: Optional[str] = None, token: Optional[str] = None, timeout: Optional[float] = None,
                 api_url: Optional[str] = None):
        self.bot = (bot or os.getenv("UNISA_RAG_BOT", "")).strip()
        if self.bot and not self.bot.startswith("@"):
            self.bot = "@" + self.bot
        self._token = (token or os.getenv("UNISA_RAG_CLIENT_TOKEN", "")).strip()
        self.timeout = float(timeout or os.getenv("UNISA_RAG_TIMEOUT") or DEFAULT_TIMEOUT)
        self.api_url = (api_url or os.getenv("UNISA_RAG_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.client_username = None
        self._offset = None
        self._lock = threading.Lock()

    def __repr__(self):  # never shows the token
        return "UnisaRag(bot=%r, timeout=%g)" % (self.bot, self.timeout)

    # --- public methods -------------------------------------------------------------------------------------------

    def query(self, question: str) -> str:
        """Text to speak for the question: the cleaned answer of the bot, or FALLBACK_ANSWER."""
        if not question or not question.strip():
            return EMPTY_QUESTION_ANSWER
        try:
            return clean_for_speech(self.ask(question)) or FALLBACK_ANSWER
        except UnisaRagError as error:
            logger.warning("[UNISA_RAG] No answer: %s", error)
        except Exception:
            logger.exception("[UNISA_RAG] Unexpected error")
        return FALLBACK_ANSWER

    async def query_async(self, question: str) -> str:
        """Same as query(), for asynchronous code: the request runs in a worker thread, the event loop stays free."""
        return await asyncio.get_running_loop().run_in_executor(None, self.query, question)

    def ask(self, question: str) -> str:
        """Answer of the bot as it wrote it, without the sources. Raises UnisaRagError."""
        with self._lock:
            self._setup()
            logger.info("[UNISA_RAG] Question of %d characters sent to the bot", len(question))
            reply = self._exchange(question)
        check_bot_reply(reply)
        return reply

    async def ask_async(self, question: str) -> str:
        """Same as ask(), for asynchronous code."""
        return await asyncio.get_running_loop().run_in_executor(None, self.ask, question)

    def check(self) -> str:
        """Verify the token and the settings; return the username of the client bot. Raises UnisaRagError."""
        with self._lock:
            self._setup()
        return self.client_username

    def close(self) -> None:
        """Nothing to release (no open connection between requests); kept so that callers can always call it."""

    # --- Telegram Bot API -----------------------------------------------------------------------------------------

    def _call(self, method: str, params: Optional[dict] = None, wait: float = 0.0):
        """Call a method of the Bot API and return its result. Errors never contain the token."""
        url = "%s/bot%s/%s" % (self.api_url, self._token, method)
        data = json.dumps(params or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=wait + HTTP_MARGIN) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except Exception:
                body = {"ok": False, "error_code": error.code, "description": str(error.reason)}
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise UnisaRagError("cannot reach Telegram (%s): %s" % (method, getattr(error, "reason", error))) from None
        if body.get("ok"):
            return body.get("result")
        code, description = body.get("error_code"), body.get("description", "")
        if code == 401:
            raise UnisaRagError("Telegram refused the token of the client bot (UNISA_RAG_CLIENT_TOKEN)")
        if code == 409:
            raise UnisaRagError("another program is reading the messages of the client bot (same token used twice, "
                                "or a webhook): %s" % description)
        if method == "sendMessage":
            raise UnisaRagError("the message to %s was refused: %s. Check UNISA_RAG_BOT and that Bot to Bot "
                                "Communication is on for both bots in @BotFather" % (self.bot, description))
        raise UnisaRagError("%s failed: %s %s" % (method, code, description))

    def _setup(self) -> None:
        """First use: check the settings and the token, forget the messages that arrived before this client."""
        if self.client_username is not None:
            return
        missing = [name for name, value in (("UNISA_RAG_CLIENT_TOKEN", self._token), ("UNISA_RAG_BOT", self.bot))
                   if not value]
        if missing:
            raise UnisaRagError("missing settings: " + ", ".join(missing))
        me = self._call("getMe")
        updates = self._call("getUpdates", {"offset": -1, "timeout": 0})  # the last one; older ones are forgotten
        self._offset = updates[-1]["update_id"] + 1 if updates else 0
        self.client_username = "@" + str(me.get("username", ""))
        logger.info("[UNISA_RAG] Client bot %s ready, RAG bot %s", self.client_username, self.bot)

    def _drain(self) -> None:
        """Drop the messages left from earlier questions (late sources, messages of other chats)."""
        while True:
            updates = self._call("getUpdates", {"offset": self._offset, "timeout": 0})
            if not updates:
                return
            self._offset = updates[-1]["update_id"] + 1
            if len(updates) < 100:
                return

    def _exchange(self, text: str) -> str:
        """Send a message to the RAG bot and return its first reply that is not a list of sources."""
        self._drain()
        sent = self._call("sendMessage", {"chat_id": self.bot, "text": text})
        chat_id, question_id = sent["chat"]["id"], sent["message_id"]
        started = time.monotonic()
        deadline = started + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UnisaRagError("no answer from the bot within %g s" % self.timeout)
            poll = max(1, min(POLL_SECONDS, int(remaining)))
            updates = self._call("getUpdates", {"offset": self._offset, "timeout": poll,
                                                "allowed_updates": ["message"]}, wait=poll)
            answer = None
            for update in updates:
                self._offset = update["update_id"] + 1
                message = update.get("message") or {}
                if (answer is None and (message.get("chat") or {}).get("id") == chat_id
                        and is_answer(message.get("message_id", 0), message.get("text") or "", question_id)):
                    answer = message.get("text") or ""
            if answer is not None:
                logger.info("[UNISA_RAG] Answer of %d characters in %.1f s", len(answer), time.monotonic() - started)
                return answer


# ---------------------------------------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Client of the RAG bot: check the settings, or ask one question.")
    parser.add_argument("--check", action="store_true", help="verify the token and print the client bot's username")
    parser.add_argument("--question", help="send one question and print the text for the voice")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    rag = UnisaRag()
    if args.check:
        print("Client bot %s, RAG bot %s" % (rag.check(), rag.bot))
    if args.question:
        started = time.monotonic()
        print(rag.query(args.question))
        print("(%.1f s)" % (time.monotonic() - started))
    if not args.check and not args.question:
        parser.print_help()


if __name__ == "__main__":
    main()
