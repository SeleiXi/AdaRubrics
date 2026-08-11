@echo off
REM tb_smoke.bat - run terminal-bench smoke test with CodeBuddy agent
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1
set PYTHONPATH=C:\Users\Karsa\Documents\AutoMetric\AdaRubrics\scripts
C:\Users\Karsa\Documents\AutoMetric\tmp\runtime\tb-venv\Scripts\tb.exe run --agent-import-path codebuddy_agent:CodeBuddyAgent --agent-kwarg model_name=hy3 --dataset-path "C:\Users\Karsa\Documents\AutoMetric\tmp\tb-repo\tasks" --task-id "broken-python" --output-path "C:\Users\Karsa\Documents\AutoMetric\runs\tb-smoke4" --n-concurrent 1 --no-upload-results
