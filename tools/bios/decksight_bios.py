#!/usr/bin/env python3
"""Rebuild the DeckSight r04 BIOS from the stock F7A0133_sign.fd.

Pipeline (each step mirrors what was done to produce r04):
  1. Unpack the stock Insyde capsule (.fd) and pull out BIOSIMG (16 MiB flash image).
  2. Replace the boot splash PNG (FFS file F0DA323C..., section 0x19) with
     BiosMaker/UEFIReplace. This recompresses both A/B DXE volumes; the
     UEFIReplace 0.28.0 LZMA encoder is required for byte-identical output.
  3. Patch both EC firmware copies (0x00000 and 0x40000) with the DeckSight
     panel tables, then fix the EC checksum.
  4. Append " DS" to the $BVDT$ BIOS version strings.
  5. Repack BIOSIMG into the capsule, fix the chunk sizes, PE checksums and
     security directories, and attach signatures.

Signatures: r04 is signed with Insyde's "QA Certificate." key (BIOSCER is an
RSA-SHA256 signature over BIOSIMG; the inner DRV_IMG PE and the outer flasher PE
each carry an Authenticode signature). All three are deterministic. Without that
private key, `--signatures-from` splices the signatures from an existing signed
.fd and verifies that each one really covers the rebuilt content.
"""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UEFIREPLACE = os.path.join(REPO, "BiosMaker", "UEFIReplace")
SPLASH_GUID = "F0DA323C-43A4-48DB-AEFE-CB314F7F5F6E"
DEFAULT_SPLASH = os.path.join(REPO, "bios", "extracted_logos", "deck_01_png.png")

EC_BASES = (0x00000, 0x40000)
EC_SIZE = 0x20000

# ---------------------------------------------------------------------------
# EC patch data (DeckSight r04)
# ---------------------------------------------------------------------------

# 256-byte EDID (base block + CTA-861 extension with HDR static metadata),
# extracted verbatim from r04. The first 128 bytes are also written over the
# stock panel EDID at 0x68e5.
R04_EDID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "r04_edid.bin")
with open(R04_EDID_FILE, "rb") as _f:
    R04_EDID = _f.read()
assert len(R04_EDID) == 256

EDID_STOCK_OFFSET = 0x68e5   # stock Deck panel EDID, overwritten with R04_EDID[:128]
EDID_EXT_OFFSET = 0x7dcf     # free (0xff) space, receives the full 256-byte EDID

# DPCD init: (slave_id, offset, value) triples, terminated by ff ff ff.
DPCD_OFFSET = 0x6965
R04_DPCD = [
    (0x10, 0x01, 0x0a),  # MAX_LINK_RATE 2.7 Gbps  (stock 0x06)
    (0x10, 0x02, 0x82),  # MAX_LANE_COUNT 2 + enhanced framing
    (0x11, 0x00, 0x14),  # LINK_BW_SET               (stock 0x06)
    (0x11, 0x01, 0x82),
    (0x11, 0x03, 0x01),
    (0x11, 0x04, 0x01),
    (0x11, 0x05, 0x01),
    (0x11, 0x06, 0x01),
]

# OCM/SPI panel registers: (register, value) pairs, terminated by ff ff.
SPI_OFFSET = 0x69ae
H_ACTIVE, HFP, HSYNC, HBP = 1080, 136, 1, 24
V_ACTIVE, VFP, VSYNC, VBP = 1920, 20, 1, 15
FRAME_RATE = 80
R04_SPI = [
    (0xa0, H_ACTIVE & 0xff), (0xa1, H_ACTIVE >> 8),
    (0xa2, HFP & 0xff), (0xa3, HFP >> 8),
    (0xa4, HSYNC & 0xff), (0xa5, HSYNC >> 8),
    (0xa6, HBP & 0xff), (0xa7, HBP >> 8),
    (0xa8, V_ACTIVE & 0xff), (0xa9, V_ACTIVE >> 8),
    (0xaa, VFP & 0xff), (0xab, VFP >> 8),
    (0xac, VSYNC & 0xff), (0xad, VSYNC >> 8),
    (0xae, VBP & 0xff), (0xaf, VBP >> 8),
    (0x9d, FRAME_RATE),   # SW_PANEL_FRAME_RATE
    (0xb0, 0x0c),         # SW_PANEL_INFO_0
    (0xb1, 0x44),         # SW_PANEL_INFO_1 (stock 0x48)
    (0x9f, 0x7b),         # MISC_NOTIFY_OCM1
    (0x9e, 0xc0),         # MISC_NOTIFY_OCM0
]

# MIPI port command table: (register, 32-bit value) records, 5 bytes each.
# 0x6c GEN_HDR: value = 00 | wc_hi/param2 | wc_lo/param1 | data type
# 0x70 GEN_PLD_DATA: payload bytes for the following long write
# 0xaa DELAY (ms), 0xff END
MIPI_OFFSET = 0x6e55
GEN_HDR, GEN_PLD, DELAY, END = 0x6c, 0x70, 0xaa, 0xff
R04_MIPI = [
    (GEN_PLD, 0x00a5a59c), (GEN_HDR, 0x00000339),   # 9c a5 a5
    (GEN_HDR, 0x00001105),                          # exit_sleep_mode
    (DELAY, 60),
    (GEN_HDR, 0x00034815),                          # 0x48 = 0x03
    (GEN_HDR, 0x00695315),                          # write_control_display 0x69
    (GEN_PLD, 0x00000951), (GEN_PLD, 0x00000000),
    (GEN_HDR, 0x00000639),                          # set_display_brightness (6 bytes)
    (GEN_HDR, 0x00003405),                          # set_tear_off
    (GEN_PLD, 0x00100044), (GEN_HDR, 0x00000339),   # set_tear_scanline
    (GEN_HDR, 0x00003505),                          # set_tear_on
    (GEN_HDR, 0x00000107),
    (GEN_HDR, 0x00000007),
    (GEN_PLD, 0x005a5afd), (GEN_HDR, 0x00000339),   # fd 5a 5a
    (GEN_HDR, 0x00029f15),                          # 0x9f = 0x02
    (GEN_PLD, 0x003000ed), (GEN_HDR, 0x00000339),   # ed 00 30
    (GEN_HDR, 0x00019f15),                          # 0x9f = 0x01
    (GEN_PLD, 0x000010b4), (GEN_PLD, 0x00001003),
    (GEN_HDR, 0x00000639),
    (GEN_HDR, 0x00002905),                          # set_display_on
    (END, 0),
]

# 8051 code patches: (offset, stock bytes, new bytes)
R04_CODE = [
    (0xf2d2, b"\x02", b"\x01"),          # display_id 2 -> 1 (DeckHD patch)
    (0xf37d, b"\x68\xe5", b"\x7d\xcf"),  # MOV DPTR,#EDID -> 256-byte EDID at 0x7dcf
    (0xf3ad, b"\x7f", b"\xff"),          # brightness clamp 0x7f -> 0xff
]

EC_CHECKSUM_OFFSET = 0x1f7fe

VERSION_TAG = b"$BVDT$"
VERSION_SUFFIX = b" DS"


def mipi_bytes(records, byteswap):
    out = bytearray()
    for reg, val in records:
        raw = val.to_bytes(4, "big")
        if byteswap:
            raw = raw[::-1]
        out += bytes([reg]) + raw
    return bytes(out)


def patch_ec(ec, copy_index, replicate_swap_bug):
    ec = bytearray(ec)
    ec[EDID_STOCK_OFFSET:EDID_STOCK_OFFSET + 128] = R04_EDID[:128]
    ec[EDID_EXT_OFFSET:EDID_EXT_OFFSET + 256] = R04_EDID

    dpcd = b"".join(bytes(t) for t in R04_DPCD) + b"\xff\xff\xff"
    ec[DPCD_OFFSET:DPCD_OFFSET + len(dpcd)] = dpcd

    spi = b"".join(bytes(t) for t in R04_SPI) + b"\xff\xff"
    ec[SPI_OFFSET:SPI_OFFSET + len(spi)] = spi

    # DeckHD's patcher.cpp byte-swaps its global init table in place on every
    # patchEC() call, so the second EC copy receives little-endian values. r04
    # has the same defect; reproduce it to stay byte-identical.
    swapped = replicate_swap_bug and copy_index == 1
    mipi = mipi_bytes(R04_MIPI, byteswap=swapped)
    ec[MIPI_OFFSET:MIPI_OFFSET + len(mipi)] = mipi

    for off, old, new in R04_CODE:
        if ec[off:off + len(old)] != old:
            sys.exit(f"EC copy {copy_index}: unexpected bytes at {off:#x} "
                     f"({ec[off:off + len(old)].hex()} != {old.hex()}); not a stock F7A0133 EC?")
        ec[off:off + len(new)] = new

    checksum = sum(ec[0x2000:EC_CHECKSUM_OFFSET]) & 0xffff
    ec[EC_CHECKSUM_OFFSET:EC_CHECKSUM_OFFSET + 2] = checksum.to_bytes(2, "big")
    return bytes(ec)


def tag_version(img):
    img = bytearray(img)
    pos, count = 0, 0
    while True:
        pos = img.find(VERSION_TAG, pos)
        if pos < 0:
            break
        # $BVDT$ ... $<version>\0 : the version string starts 13 bytes in
        start = pos + 13
        end = img.index(0, start)
        img[end:end + len(VERSION_SUFFIX)] = VERSION_SUFFIX
        pos, count = end, count + 1
    if count != 2:
        sys.exit(f"expected 2 $BVDT$ version strings, found {count}")
    return bytes(img)


# ---------------------------------------------------------------------------
# Insyde capsule (.fd) handling
# ---------------------------------------------------------------------------

def find_chunks(fd):
    chunks, i = {}, 0
    while True:
        j = fd.find(b"$_IFLASH_", i)
        if j < 0:
            return chunks
        name = fd[j + 9:j + 16].decode()
        _, data_size = struct.unpack("<II", fd[j + 16:j + 24])
        chunks[name] = (j, j + 24, data_size)
        i = j + 1


def pe_info(buf, base):
    pe = base + struct.unpack("<I", buf[base + 0x3c:base + 0x40])[0]
    opt = pe + 24
    nsec = struct.unpack("<H", buf[pe + 6:pe + 8])[0]
    sec_table = opt + struct.unpack("<H", buf[pe + 20:pe + 22])[0]
    return {
        "opt": opt,
        "size_of_image": opt + 56,
        "checksum": opt + 64,
        "secdir": opt + 112 + 4 * 8,
        "sections": [sec_table + 40 * k for k in range(nsec)],
    }


def pe_checksum(buf, base, length, checksum_field):
    total = 0
    data = bytes(buf[base:base + length])
    if len(data) % 2:
        data += b"\0"
    rel = checksum_field - base
    for (word,) in struct.iter_unpack("<H", data):
        total += word
        total = (total & 0xffff) + (total >> 16)
    for (word,) in struct.iter_unpack("<H", data[rel:rel + 4]):  # remove checksum field
        total -= word
        if total < 0:
            total += 0xffff
    total = (total & 0xffff) + (total >> 16)
    return (total + length) & 0xffffffff


def authenticode_hash(buf, base, length):
    """Authenticode PE image hash (SHA-256) of the PE at buf[base:base+length]."""
    info = pe_info(buf, base)
    cert_off, cert_len = struct.unpack("<II", buf[info["secdir"]:info["secdir"] + 8])
    h = hashlib.sha256()
    h.update(buf[base:info["checksum"]])
    h.update(buf[info["checksum"] + 4:info["secdir"]])
    size_of_headers = struct.unpack("<I", buf[info["opt"] + 60:info["opt"] + 64])[0]
    h.update(buf[info["secdir"] + 8:base + size_of_headers])
    end = size_of_headers
    for s in sorted(info["sections"], key=lambda s: struct.unpack("<I", buf[s + 20:s + 24])[0]):
        raw_size, raw_ptr = struct.unpack("<II", buf[s + 16:s + 24])
        if raw_size:
            h.update(buf[base + raw_ptr:base + raw_ptr + raw_size])
            end = max(end, raw_ptr + raw_size)
    if end < cert_off:
        h.update(buf[base + end:base + cert_off])
    return h.digest()


def verify_rsa_sha256(sig, message_digest, modulus):
    decoded = pow(int.from_bytes(sig, "big"), 65537, modulus).to_bytes(len(sig), "big")
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    return decoded.startswith(b"\x00\x01\xff") and decoded.endswith(prefix + message_digest)


def certificate_modulus(pkcs7_der):
    with tempfile.TemporaryDirectory() as tmp:
        p7 = os.path.join(tmp, "sig.p7")
        pem = os.path.join(tmp, "cert.pem")
        with open(p7, "wb") as f:
            f.write(pkcs7_der)
        subprocess.run(["openssl", "pkcs7", "-inform", "DER", "-in", p7, "-print_certs", "-out", pem],
                       check=True, capture_output=True)
        out = subprocess.run(["openssl", "x509", "-in", pem, "-noout", "-modulus"],
                             check=True, capture_output=True, text=True).stdout
    return int(out.strip().split("=", 1)[1], 16)


def authenticode_digest(pkcs7_der):
    """The PE image hash inside SpcIndirectDataContent (DigestInfo OCTET STRING)."""
    marker = bytes.fromhex("3031300d0609608648016503040201050004 20".replace(" ", ""))
    i = pkcs7_der.index(marker)
    return pkcs7_der[i + len(marker):i + len(marker) + 32]


def win_cert(buf, secdir):
    off, size = struct.unpack("<II", buf[secdir:secdir + 8])
    return off, size


def repack(stock_fd, biosimg, sig_source):
    fd = bytearray(stock_fd)
    chunks = find_chunks(fd)
    drv_hdr, drv_data, _ = chunks["DRV_IMG"]
    inner = pe_info(fd, drv_data)
    outer = pe_info(fd, 0)

    _, img_data, img_size = chunks["BIOSIMG"]
    assert img_size == len(biosimg)
    fd[img_data:img_data + img_size] = biosimg

    src = bytearray(sig_source)
    src_chunks = find_chunks(src)
    src_inner = pe_info(src, src_chunks["DRV_IMG"][1])
    src_outer = pe_info(src, 0)

    # BIOSCER: RSA-SHA256 over BIOSIMG
    _, cer_data, cer_size = src_chunks["BIOSCER"]
    bioscer = bytes(src[cer_data:cer_data + cer_size])
    fd[chunks["BIOSCER"][1]:chunks["BIOSCER"][1] + cer_size] = bioscer

    # Inner (DRV_IMG) Authenticode blob sits at the end of the inner PE.
    inner_cert_rel, _ = win_cert(fd, inner["secdir"])
    src_inner_rel, src_inner_len = win_cert(src, src_inner["secdir"])
    inner_blob = bytes(src[src_chunks["DRV_IMG"][1] + src_inner_rel:
                           src_chunks["DRV_IMG"][1] + src_inner_rel + src_inner_len])
    inner_start = drv_data + inner_cert_rel
    outer_cert_off, _ = win_cert(fd, outer["secdir"])
    src_outer_off, src_outer_len = win_cert(src, src_outer["secdir"])
    outer_blob = bytes(src[src_outer_off:src_outer_off + src_outer_len])

    # Layout: [.. inner PE body ..][inner cert][outer cert]; everything before
    # the inner cert keeps its stock position.
    new_inner_len = inner_cert_rel + len(inner_blob)
    body = fd[:inner_start] + inner_blob
    new_outer_cert_off = len(body)
    fd = bytearray(body + outer_blob)

    # Inner PE headers
    struct.pack_into("<I", fd, inner["secdir"] + 4, len(inner_blob))
    # Outer PE: .reloc section wraps the DRV_IMG chunk
    reloc = outer["sections"][-1]
    reloc_ptr = struct.unpack("<I", fd[reloc + 20:reloc + 24])[0]
    reloc_size = new_outer_cert_off - reloc_ptr
    struct.pack_into("<II", fd, reloc + 8, reloc_size, struct.unpack("<I", fd[reloc + 12:reloc + 16])[0])
    struct.pack_into("<I", fd, reloc + 16, reloc_size)
    struct.pack_into("<I", fd, outer["size_of_image"], new_outer_cert_off)
    struct.pack_into("<II", fd, outer["secdir"], new_outer_cert_off, len(outer_blob))

    # Chunk size fields: data_size, and "file size" = distance to whatever follows.
    struct.pack_into("<II", fd, drv_hdr + 16, new_inner_len, new_inner_len)
    order = sorted((v[0], k) for k, v in chunks.items() if k != "DRV_IMG")
    for idx, (hdr, name) in enumerate(order):
        data_start = hdr + 24
        nxt = order[idx + 1][0] if idx + 1 < len(order) else inner_start
        struct.pack_into("<I", fd, hdr + 16, nxt - data_start)

    struct.pack_into("<I", fd, inner["checksum"],
                     pe_checksum(fd, drv_data, new_inner_len, inner["checksum"]))
    # The signer computes both checksums with the certificate already attached.
    struct.pack_into("<I", fd, outer["checksum"],
                     pe_checksum(fd, 0, len(fd), outer["checksum"]))

    # Verify that the spliced signatures really cover this content.
    mod_inner = certificate_modulus(inner_blob[8:])
    mod_outer = certificate_modulus(outer_blob[8:])
    checks = [
        ("BIOSCER (RSA-SHA256 over BIOSIMG)",
         verify_rsa_sha256(bioscer, hashlib.sha256(biosimg).digest(), mod_inner)),
        ("inner DRV_IMG Authenticode image hash",
         authenticode_digest(inner_blob[8:]) == authenticode_hash(fd, drv_data, new_inner_len)),
        ("outer flasher Authenticode image hash",
         authenticode_digest(outer_blob[8:]) == authenticode_hash(fd, 0, new_outer_cert_off)),
    ]
    return bytes(fd), checks


def run_uefireplace(biosimg, splash, workdir):
    src = os.path.join(workdir, "BIOSIMG.bin")
    dst = os.path.join(workdir, "BIOSIMG_SPLASH.bin")
    png = os.path.join(workdir, "splash.png")
    with open(src, "wb") as f:
        f.write(biosimg)
    shutil.copy(splash, png)
    cmd = [UEFIREPLACE, src, SPLASH_GUID, "0x19", png, "-o", dst, "-all"]
    # UEFIReplace needs the host's Qt5Core; step out of the flatpak sandbox if we are in one.
    if os.path.exists("/.flatpak-info"):
        cmd = ["flatpak-spawn", "--host"] + cmd
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(dst):
        sys.exit(f"UEFIReplace failed:\n{res.stdout}{res.stderr}")
    with open(dst, "rb") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stock_fd", help="stock F7A0133_sign.fd")
    ap.add_argument("output", help="output .fd (or .bin with --biosimg-only)")
    ap.add_argument("--splash", default=DEFAULT_SPLASH, help="replacement boot splash PNG")
    ap.add_argument("--signatures-from", metavar="SIGNED_FD",
                    help="splice and verify BIOSCER/Authenticode signatures from this signed .fd")
    ap.add_argument("--biosimg-only", action="store_true", help="write the raw 16 MiB flash image instead of a .fd")
    ap.add_argument("--fix-ec2-swap", action="store_true",
                    help="write the MIPI table correctly into EC copy 2 (NOT byte-identical to r04)")
    ap.add_argument("--work-dir", default=os.path.expanduser("~/.cache/decksight-bios"),
                    help="scratch dir visible to the host (for UEFIReplace)")
    args = ap.parse_args()

    stock_fd = open(args.stock_fd, "rb").read()
    chunks = find_chunks(stock_fd)
    _, img_data, img_size = chunks["BIOSIMG"]
    biosimg = stock_fd[img_data:img_data + img_size]
    print(f"[1] BIOSIMG extracted: {img_size:#x} bytes, sha256 {hashlib.sha256(biosimg).hexdigest()[:16]}")

    os.makedirs(args.work_dir, exist_ok=True)
    biosimg = run_uefireplace(biosimg, args.splash, args.work_dir)
    print(f"[2] splash replaced with {os.path.basename(args.splash)} (DXE volumes recompressed)")

    img = bytearray(biosimg)
    for idx, base in enumerate(EC_BASES):
        img[base:base + EC_SIZE] = patch_ec(img[base:base + EC_SIZE], idx, not args.fix_ec2_swap)
    print("[3] EC copies patched" + (" (EC2 MIPI table fixed)" if args.fix_ec2_swap else " (EC2 swap bug reproduced)"))

    img = tag_version(bytes(img))
    print(f"[4] version strings tagged; BIOSIMG sha256 {hashlib.sha256(img).hexdigest()}")

    if args.biosimg_only:
        out = img
    else:
        if not args.signatures_from:
            sys.exit("a signed .fd needs signatures: pass --signatures-from, or use --biosimg-only")
        out, checks = repack(stock_fd, img, open(args.signatures_from, "rb").read())
        print("[5] capsule repacked; signature checks:")
        for name, ok in checks:
            print(f"      {'PASS' if ok else 'FAIL'}  {name}")
        if not all(ok for _, ok in checks):
            sys.exit("signature verification failed: the spliced signatures do not cover this content")

    with open(args.output, "wb") as f:
        f.write(out)
    print(f"wrote {args.output} ({len(out):#x} bytes) sha256 {hashlib.sha256(out).hexdigest()}")


if __name__ == "__main__":
    main()
