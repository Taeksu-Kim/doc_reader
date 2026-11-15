#!/usr/bin/env bash
# OCR option servers for `doc_parser --ocr-url`:  bash tools/ocr.sh start|stop|status
#   vLLM  :8091  PaddleOCR-VL-1.6 text generation (weights ~2GB + KV fixed at 1GiB)
#   paddle :8090 layout detection + doc_parser protocol (tools/ocr_server.py)
# WSL notes: localhost loopback is unreliable -> use the WSL IP; paddle needs libcuda from
# /usr/lib/wsl/lib; vLLM needs TRITON attention + eager (no nvcc for flashinfer JIT).
# PIDs go to files: `pkill -f` would also match (and kill) the calling shell.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="${OCR_RUN_DIR:-$HOME/.cache/doc_parser_ocr}"; mkdir -p "$RUN"
IP="$(hostname -I | awk '{print $1}')"
MODEL_DIR="${OCR_MODEL_DIR:-$HOME/.paddlex/official_models/PaddleOCR-VL-1.6}"
VLLM="$HOME/miniconda3/envs/vllm/bin/vllm"
PADDLE_PY="$HOME/miniconda3/envs/paddleocr/bin/python"

up() { "$PADDLE_PY" -c "import urllib.request,sys;urllib.request.urlopen('$1',timeout=3)" 2>/dev/null; }
wait_up() { for _ in $(seq 120); do up "$1" && return 0; sleep 3; done; echo "기동 실패: $2 로그 $RUN/$2.log" >&2; exit 1; }

case "${1:-status}" in
start)
  if [ ! -d "$MODEL_DIR" ]; then   # first run: the paddle pipeline downloads both models
    LD_LIBRARY_PATH=/usr/lib/wsl/lib "$PADDLE_PY" -c "from paddleocr import PaddleOCRVL; PaddleOCRVL(pipeline_version='v1.6')"
  fi
  if ! up "http://$IP:8091/v1/models"; then
    VLLM_USE_FLASHINFER_SAMPLER=0 PYTHONUNBUFFERED=1 nohup "$VLLM" serve "$MODEL_DIR" \
      --served-model-name PaddleOCR-VL-1.6-0.9B --trust-remote-code --gpu-memory-utilization 0.9 --kv-cache-memory-bytes 1073741824 \
      --max-model-len 16384 --max-num-batched-tokens 16384 --no-enable-prefix-caching --mm-processor-cache-gb 0 \
      --attention-backend TRITON_ATTN --host 0.0.0.0 --port 8091 > "$RUN/vllm.log" 2>&1 &
    echo $! > "$RUN/vllm.pid"
    wait_up "http://$IP:8091/v1/models" vllm
  fi
  if ! up "http://$IP:8090/health"; then
    LD_LIBRARY_PATH=/usr/lib/wsl/lib PYTHONUNBUFFERED=1 nohup "$PADDLE_PY" "$HERE/ocr_server.py" --port 8090 \
      --vl-url "http://$IP:8091/v1" > "$RUN/paddle.log" 2>&1 &
    echo $! > "$RUN/paddle.pid"
    wait_up "http://$IP:8090/health" paddle
  fi
  echo "준비됨: --ocr-url http://$IP:8090" ;;
stop)
  for n in paddle vllm; do
    if [ -f "$RUN/$n.pid" ]; then kill "$(cat "$RUN/$n.pid")" 2>/dev/null || true; rm -f "$RUN/$n.pid"; fi
  done
  echo "중지함" ;;
status)
  up "http://$IP:8091/v1/models" && echo "vllm  :8091 up" || echo "vllm  :8091 down"
  up "http://$IP:8090/health" && echo "paddle :8090 up  → --ocr-url http://$IP:8090" || echo "paddle :8090 down" ;;
*) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
