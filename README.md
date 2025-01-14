# GPUtopia Queenbee

Allows multiple AI workers to register and make their services available.

Routes user requests to workers.

User requests are `/v1/embeddings`, `/v1/chat/completion` , with openai syntax.

Models must be in the format:  `hf-user/hf-repo:filter`

Only GGML/GGUF supported for now.

Registration as a worker is "open to all", no need to sign up.  The first job will always be free (proof of work).


Workflow documented here:

https://github.com/ArcadeLabsInc/workerbee/wiki

## Tech stack
- Python
- FastAPI

## Running locally

```
git clone git@github.com:ArcadeLabsInc/queenbee.git
cd queenbee
poetry install
poetry run uvicorn ai_spider:app --reload --log-config logging.conf
```

## Running tests

```
pytest
```

