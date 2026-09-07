# SeedVR2 cross-platform quality patches

This directory contains a project-maintained patch for the Apache-2.0 licensed
`numz/ComfyUI-SeedVR2_VideoUpscaler` runner.

- Upstream repository: https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler
- Pinned revision: `4490bd1f482e026674543386bb2a4d176da245b9`
- Upstream version: `2.5.24`
- Source archive SHA-256: `04c61842bc00fd8673e6bc9a3b1b1935955461f363791070ed14d67d2a2e77fb`
- Patch SHA-256: `bd92759faf0523658cf24ab280139a9217064cd21ae9d6b00e7f0992982a8775`
- Color-input patch SHA-256: `bb6ce72648ed8f175cab10179d1af90645e638b17296ddb2e2ff45e89f79215c`

The patch changes the upstream runner to:

- compute MPS-sensitive RMSNorm and SDPA operations in float32;
- move MPS tensor tile/concatenation operations that exceed INT_MAX to CPU;
- reject non-finite latent and final tensors;
- preserve ten-bit precision through an RGB48LE pipe;
- convert sRGB model output explicitly to BT.709 limited with `zscale` and
  error-diffusion dithering, then encode it as HEVC with `libx265`, `preset slow`,
  `CRF 10`, and the `hvc1` tag;
- remove artificial lead-in frames from the single-device streaming path; and
- cache successful model SHA-256 validation after the first download.

The MPS numerical workarounds remain conditional on `device.type == "mps"`.
The precision, validation, streaming, color, and output changes are also used by
the CUDA 13.0 RTX 5090 path.

The additional `seedvr2-color-input.patch` adds a streaming FFmpeg reader for
trusted, application-generated filter graphs. It sends `bgr48le` frames directly
to the model without a temporary mezzanine file, so the application can normalize
full-range, non-BT.709, RGB, high-bit-depth, and tone-mapped HDR sources without
the upstream OpenCV reader first reducing them to 8-bit. The reader runs to FFmpeg
EOF, preserves exact rational frame rates, and bounds/reaps FFmpeg subprocesses on
success and failure so a full stderr pipe or stuck child cannot hang the runner.

The application downloads the exact source archive at first use, verifies its
SHA-256 digest, and applies `seedvr2-mps-quality.patch` followed by
`seedvr2-color-input.patch`. The downloaded source
keeps its original copyright notices and Apache-2.0 license.

SeedVR2 model files are downloaded separately from
https://huggingface.co/numz/SeedVR2_comfyUI and checked against the SHA-256
digests recorded in `app/ai_enhance.py`. Model weights are not redistributed in
this repository.

See `SEEDVR2_LICENSE.txt` for the upstream Apache License 2.0 terms. All changes
in this directory are explicitly marked as modifications made for Local Video
Cutter.
