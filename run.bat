@ECHO OFF
:: This batch file build the sisb/api-library-epfl image and run the api-library-epfl container
set mypath=%cd%

docker build --progress=plain -t smartbiblia/agentdesk-runner:latest .

ECHO ============================
ECHO Image builded...
ECHO ============================

docker run -d --name agentdesk-runner -p 8000:8000 -v %mypath%/runner:/app/runner smartbiblia/agentdesk-runner:latest

ECHO ============================
ECHO Container running...
ECHO Open http://localhost:8000

ECHO ============================
PAUSE