# UnisaRag client

Telegram client of the RAG assistant of the University of Salerno, for the Rasa actions of the ARI robot.

`UnisaRag.query(text)` sends the question (the text produced by the robot's speech recognition) to the Telegram bot
of the RAG system, waits for the answer, drops the message with the sources and returns the answer cleaned for
speech synthesis: no markdown, lists turned into sentences, web and e-mail addresses and phone numbers written the way
they are read aloud. How the text then reaches the text-to-speech service is up to the caller.

The client is itself a Telegram bot and talks to the RAG bot through the Telegram Bot API (bot-to-bot communication,
Bot API 10.0): it needs only a bot token, no phone number or login.

| File | Content |
|---|---|
| `unisa_rag.py` | The client, to copy into `rasa_ws/actions/` |
| `test_unisa_rag.py` | Tests without Rasa: offline, with a fake Telegram on the PC, and against the real bot |

## Requirements

- Python 3.8 or later. Only the standard library: nothing to install.
- Outbound HTTPS access to `api.telegram.org` (a proxy is taken from `HTTPS_PROXY`).
- The RAG bot running, without access code.

## Set-up

1. In Telegram, create the client bot with `/newbot` in @BotFather: it gives the bot's token.
2. In @BotFather turn on **Bot to Bot Communication** for both bots, the client bot and the RAG bot (bot settings in
   the BotFather mini app; in some versions also `/mybots` → the bot → Bot Settings).
3. Set the environment variables where the program starts (for Rasa: where `rasa run actions` starts):

   ```bash
   export UNISA_RAG_CLIENT_TOKEN="123456789:AA..."   # token of the client bot: keep it private
   export UNISA_RAG_BOT="@name_of_the_rag_bot"
   export UNISA_RAG_TIMEOUT=90                        # optional, seconds to wait for an answer
   ```

4. Check, without Rasa:

   ```bash
   python unisa_rag.py --check
   python unisa_rag.py --question "Chi è l'attuale Rettore dell'Università di Salerno?"
   ```

## Use in a Rasa action

Copy `unisa_rag.py` into `rasa_ws/actions/`. Keep `test_unisa_rag.py` **outside** that folder: the action server
imports every module in it.

```python
from rasa_sdk import Action
from .unisa_rag import UnisaRag      # or: from actions.unisa_rag import UnisaRag

RAG = UnisaRag()                     # one instance for the whole process

class ActionUnisaRag(Action):
    def name(self):
        return "action_unisa_rag"

    async def run(self, dispatcher, tracker, domain):
        answer = await RAG.query_async(tracker.latest_message.get("text", ""))
        dispatcher.utter_message(text=answer)   # example: the answer goes on to the text-to-speech
        return []
```

Then add `action_unisa_rag` to the actions of `domain.yml`, connect it to the rules or flows, and restart the action
server and Rasa.

`query()` and `query_async()` always return a text to speak: if the bot does not answer, they return a fallback
sentence and write the reason in the log. The log never contains the text of questions and answers, nor the token.

## Tests

```bash
python test_unisa_rag.py --offline     # no Telegram: fake Bot API on 127.0.0.1, text cleaning
python test_unisa_rag.py --check       # verifies the token of the client bot
python test_unisa_rag.py               # 10 questions to the real bot, transcript in test_unisa_rag_<date>.md
```

## Notes

- Only one program at a time can read the messages of a bot: do not run two programs with the same token (for
  example the live test on a PC and the Rasa action server).
- The RAG bot accepts at most 5 messages per minute from each chat.
- It keeps the conversation history of each chat in memory: all the questions of this client are one conversation,
  until the RAG bot is restarted.
- The first answer after a pause of more than 5 minutes can take up to about 40 s, while the language model is loaded.
