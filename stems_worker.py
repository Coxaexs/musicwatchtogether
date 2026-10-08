"""Demucs stem separation worker for the DJ booth.

Runs in its own venv (stems-env, with torch + demucs) so the bot process never
imports torch. stems.py starts it and talks to it over stdin/stdout, one JSON
object per line:

    -> {"id": 1, "input": "/path/x.raw", "output": "/path/x.npy"}
    <- {"id": 1, "ok": true, "seconds": 12.3}

The input is the deck's decoded track: s16le stereo at 48 kHz. The output is
an int16 array of shape (3, n, 2) holding drums, bass and vocals at 48 kHz,
sample-aligned with the input. "Other" is not stored: the engine derives it
as the mix minus those three, so playing every stem at full is exactly the
original track.
"""

import json
import os
import sys
import time

import numpy as np
import torch
from julius import resample_frac
from demucs.apply import apply_model
from demucs.pretrained import get_model

SR = 48_000
KEEP = ("drums", "bass", "vocals")
MODEL = os.environ.get("DJ_STEMS_MODEL", "htdemucs")
# Above this the GPU is left alone for a job (a passively cooled card in a
# desktop case can overheat and drop off the bus).
GPU_MAX_TEMP = int(os.environ.get("DJ_STEMS_GPU_MAX_TEMP", "80"))
CPU_THREADS = int(os.environ.get("DJ_STEMS_CPU_THREADS", "2"))


def log(*parts):
    print("[stems]", *parts, file=sys.stderr, flush=True)


def separate(model, device, pcm):
    wav = torch.from_numpy(pcm.astype(np.float32).T / 32768.0)        # (2, n) @ 48k
    n = wav.shape[1]
    mix = resample_frac(wav, SR, model.samplerate)
    ref = mix.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(model, ((mix - mean) / std)[None], device=device,
                          shifts=1, split=True, overlap=0.25, progress=False)[0]
    out = out * std + mean                                             # (sources, 2, m)
    picked = torch.stack([out[model.sources.index(name)] for name in KEEP])
    back = resample_frac(picked.cpu(), model.samplerate, SR)          # (3, 2, ~n)
    if back.shape[-1] < n:
        back = torch.nn.functional.pad(back, (0, n - back.shape[-1]))
    back = back[..., :n].transpose(1, 2)                               # (3, n, 2)
    return (back.clamp(-1, 1) * 32767).round().to(torch.int16).numpy()


def gpu_temperature():
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.split()[0])
    except Exception:
        return None


def be_gentle():
    """Few threads, low priority: the bot renders live audio on the same
    cores, and a CPU job (no GPU, or the GPU cooling off) must not starve it."""
    torch.set_num_threads(CPU_THREADS)
    try:
        os.nice(10)
    except OSError:
        pass


def use_cpu(model):
    return model.to("cpu"), "cpu"


def main():
    # The protocol gets its own copy of stdout; anything libraries print
    # (download notices, warnings) goes to stderr, i.e. the worker log.
    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    be_gentle()
    # GPU is off by default: the passive Tesla P4 overheats without a fan.
    device = "cuda" if os.environ.get("DJ_STEMS_ALLOW_GPU") == "1" and torch.cuda.is_available() else "cpu"
    started = time.monotonic()
    model = get_model(MODEL)
    model.eval()
    if device == "cuda":
        model.to(device)
    else:
        model, device = use_cpu(model)
    log(f"{MODEL} ready on {device} in {time.monotonic() - started:.1f}s")
    print(json.dumps({"ready": True, "device": device, "model": MODEL}), file=out, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        job = json.loads(line)
        reply = {"id": job.get("id")}
        try:
            started = time.monotonic()
            pcm = np.fromfile(job["input"], dtype="<i2")
            pcm = pcm[: len(pcm) // 2 * 2].reshape(-1, 2)
            if device == "cuda":
                temp = gpu_temperature()
                if temp is not None and temp >= GPU_MAX_TEMP:
                    log(f"GPU at {temp} C, cooling off: this one runs on the CPU")
                    model.to("cpu")
                    try:
                        stems = separate(model, "cpu", pcm)
                    finally:
                        model.to("cuda")
                else:
                    try:
                        stems = separate(model, device, pcm)
                    except Exception as error:
                        if "CUDA" not in str(error) and "cuda" not in str(error):
                            raise
                        # The GPU failed (e.g. fell off the bus): carry on
                        # without it for the rest of this worker's life.
                        log("GPU failed, switching to the CPU:", repr(error)[:200])
                        # Fresh weights: copying back off a dead GPU can fail too.
                        model, device = use_cpu(get_model(MODEL).eval())
                        stems = separate(model, device, pcm)
            else:
                stems = separate(model, device, pcm)
            tmp = job["output"] + ".part"
            with open(tmp, "wb") as f:
                np.save(f, stems)
            os.replace(tmp, job["output"])
            reply.update(ok=True, seconds=round(time.monotonic() - started, 2))
        except Exception as error:  # report and keep serving
            log("failed:", repr(error))
            reply.update(ok=False, error=str(error)[:300])
        finally:
            if device == "cuda":
                torch.cuda.empty_cache()
        print(json.dumps(reply), file=out, flush=True)


if __name__ == "__main__":
    main()
