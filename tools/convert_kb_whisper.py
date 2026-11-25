"""Convert a KB-Whisper style to CTranslate2 for faster-whisper.

KBLab publishes three transcription styles of each KB-Whisper model
(standard, strict, subtitle) as Hugging Face weights, but the CTranslate2
file that faster-whisper loads exists for one of them only.  This script
downloads the requested revision and converts it.  It runs in a throw-away
Docker build stage, so torch and transformers never reach the final image.

Usage: python3 convert_kb_whisper.py REPO REVISION OUTPUT_DIR [QUANTIZATION]
"""

import sys

from ctranslate2.converters import TransformersConverter
from huggingface_hub import snapshot_download


def main():
    repo, revision, out = sys.argv[1:4]
    quantization = sys.argv[4] if len(sys.argv) > 4 else "int8"
    src = snapshot_download(
        repo, revision=revision,
        allow_patterns=["*.json", "*.txt", "model.safetensors"])
    TransformersConverter(
        src, copy_files=["tokenizer.json", "preprocessor_config.json"],
    ).convert(out, quantization=quantization, force=True)
    print(f"Converted {repo}@{revision} -> {out} ({quantization})")


if __name__ == "__main__":
    main()
