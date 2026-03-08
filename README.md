# agentdesk-runner

## Install app requirements (including nanobot)

```
pip install -r requirements.txt
```

## Configure nanobot

```
vi ~/.nanobot/config.json
```

## Run

```
uvicorn runner.main:app --host 0.0.0.0 --port 8000
```

## Check

https://xxxxx-8000.app.github.dev/health

## Examples

Make a POST request on /run with

```
{
  "cmd": "nanobot agent -m 'list files in project'"
}
```