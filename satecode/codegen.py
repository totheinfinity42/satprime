import subprocess
import tempfile
from pathlib import Path


_C_SRC = r"""
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define _MAGIC    0x53415449u
#define _HDR_SZ   32
#define _BUF_SZ   (64 * 1024)

static unsigned char _buf[_BUF_SZ];

static int _read_hdr(int fd, uint64_t *boundary, uint64_t *total) {
    unsigned char h[_HDR_SZ];
    if (read(fd, h, _HDR_SZ) != _HDR_SZ) return -1;
    uint32_t m = ((uint32_t)h[0]<<24)|((uint32_t)h[1]<<16)|((uint32_t)h[2]<<8)|h[3];
    if (m != _MAGIC) return -1;
    uint64_t b = 0, t = 0;
    for (int i = 0; i < 8; i++) b = (b<<8)|h[8+i];
    for (int i = 0; i < 8; i++) t = (t<<8)|h[16+i];
    *boundary = b; *total = t;
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) return 1;
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }
    uint64_t boundary = 0, total = 0;
    if (_read_hdr(fd, &boundary, &total) != 0) {
        fprintf(stderr, "bad header\n"); close(fd); return 1;
    }
    if (argc >= 3) boundary = (uint64_t)strtoull(argv[2], NULL, 10);
    ssize_t n;
    while (boundary > 0) {
        size_t want = boundary < _BUF_SZ ? (size_t)boundary : _BUF_SZ;
        n = read(fd, _buf, want);
        if (n <= 0) break;
        boundary -= (uint64_t)n;
    }
    close(fd);
    return 0;
}
""".lstrip()

_UNIT_TMPL = """\
[Unit]
DefaultDependencies=no
After=local-fs.target
Before=containerd.service docker.service {extra_before}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={exec_path} {image_path} {boundary}

[Install]
WantedBy=sysinit.target
"""

_CC_MAP = {
    "aarch64": "aarch64-linux-gnu-gcc",
    "arm":     "arm-linux-gnueabihf-gcc",
    "x86_64":  "gcc",
    "native":  "gcc",
}


def source() -> str:
    return _C_SRC


def unit(image_path: str, boundary: int,
         exec_path: str = "/usr/local/bin/sat_primer",
         extra_before: str = "") -> str:
    return _UNIT_TMPL.format(
        image_path=image_path, boundary=boundary,
        exec_path=exec_path, extra_before=extra_before,
    )


def compile_primer(src: str, out: Path, arch: str = "native",
                   cflags: str = "-O2 -static") -> None:
    cc = _CC_MAP.get(arch)
    if not cc:
        raise ValueError("unknown arch: {}".format(arch))
    with tempfile.TemporaryDirectory() as td:
        s = Path(td) / "primer.c"
        s.write_text(src)
        r = subprocess.run([cc] + cflags.split() + ["-o", str(out), str(s)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("compile failed: {}".format(r.stderr.strip()))
    out.chmod(0o755)


def emit(output_dir: Path, image_path: str, boundary: int,
         arch: str = "native", compile_binary: bool = True,
         exec_path: str = "/usr/local/bin/sat_primer") -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    out = {}

    src_path = output_dir / "primer.c"
    src_path.write_text(source())
    out["source"] = src_path

    unit_path = output_dir / "sat-primer.service"
    unit_path.write_text(unit(image_path, boundary, exec_path))
    out["unit"] = unit_path

    if compile_binary:
        bin_path = output_dir / "sat_primer"
        compile_primer(source(), bin_path, arch)
        out["binary"] = bin_path

    return out
