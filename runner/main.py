from fastapi import FastAPI, WebSocket, Body
import subprocess
import uuid

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/run")
def run_cmd(cmd: str = Body(embed=True)):
    proc = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True
    )
    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode
    }

@app.websocket("/stream")
async def stream_cmd(ws: WebSocket):
    await ws.accept()

    data = await ws.receive_text()

    proc = subprocess.Popen(
        data,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    for line in proc.stdout:
        await ws.send_text(line)

    await ws.close()