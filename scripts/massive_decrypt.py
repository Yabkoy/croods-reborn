#!/usr/bin/env python3
import os
import sys
import shutil
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from Crypto.Cipher import AES

KEY = bytes.fromhex("5354636f51776b61544679707449394639436f53704846786d336a5457716865")
IV = b"\x00" * 16
SEVEN_Z_MAGIC = b"\x37\x7a\xbc\xaf\x27\x1c"
LUA_51_MAGIC = b"\x1bLuaQ"

JAVA_BIN = os.path.expanduser("~/tools/jdk-25/bin/java")
if not os.path.isfile(JAVA_BIN):
    JAVA_BIN = shutil.which("java") or "java"

UNLUAC_JAR = os.path.expanduser("~/tools/unluac/unluac.jar")


def decrypt_buffer(data: bytes) -> bytes | None:
    if len(data) < 16 or len(data) % 16 != 0:
        return None
    dec = AES.new(KEY, AES.MODE_CBC, iv=IV).decrypt(data)
    pad = dec[-1]
    if 1 <= pad <= 16 and dec[-pad:] == bytes([pad]) * pad:
        dec = dec[:-pad]
    return dec


def extract_all(src_root: str, dst_root: str) -> tuple[int, int]:
    extracted_archives = 0
    copied_files = 0

    for root, _, files in os.walk(src_root):
        rel_dir = os.path.relpath(root, src_root)
        target_dir = os.path.normpath(os.path.join(dst_root, rel_dir))
        os.makedirs(target_dir, exist_ok=True)

        for name in files:
            src_path = os.path.join(root, name)
            with open(src_path, "rb") as f:
                raw = f.read()

            dec = decrypt_buffer(raw)

            if dec is not None and dec.startswith(SEVEN_Z_MAGIC):
                with tempfile.NamedTemporaryFile(suffix=".7z", delete=False) as tmp:
                    tmp.write(dec)
                    tmp_path = tmp.name
                try:
                    subprocess.run(
                        ["7z", "x", "-y", f"-o{target_dir}", tmp_path],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    extracted_archives += 1
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            elif dec is not None and dec.startswith(LUA_51_MAGIC):
                with open(os.path.join(target_dir, name), "wb") as out:
                    out.write(dec)
                copied_files += 1
            else:
                shutil.copy2(src_path, os.path.join(target_dir, name))
                copied_files += 1

    return extracted_archives, copied_files


def decompile_one(lua_path: str) -> tuple[str, bool, str]:
    res = subprocess.run(
        [JAVA_BIN, "-jar", UNLUAC_JAR, lua_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if res.returncode == 0 and res.stdout:
        with open(lua_path, "wb") as f:
            f.write(res.stdout)
        return lua_path, True, ""
    err = res.stderr.decode("utf-8", errors="replace").strip()
    return lua_path, False, err


def decompile_tree(dst_root: str) -> tuple[int, list[tuple[str, str]]]:
    bytecode_files = []
    for root, _, files in os.walk(dst_root):
        for name in files:
            full_path = os.path.join(root, name)
            try:
                with open(full_path, "rb") as f:
                    header = f.read(5)
                if header == LUA_51_MAGIC:
                    bytecode_files.append(full_path)
            except OSError:
                pass

    ok_count = 0
    failures = []
    workers = min(8, os.cpu_count() or 4)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(decompile_one, p) for p in bytecode_files]
        for fut in as_completed(futures):
            path, ok, err = fut.result()
            if ok:
                ok_count += 1
            else:
                failures.append((path, err))

    return ok_count, failures


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Użycie: python3 {sys.argv[0]} <katalog_z_gry> <katalog_wyjsciowy>")
        sys.exit(1)

    src_root = os.path.abspath(sys.argv[1])
    dst_root = os.path.abspath(sys.argv[2])

    if not os.path.isdir(src_root):
        print(f"Błąd: katalog wejściowy nie istnieje: {src_root}")
        sys.exit(1)

    if not os.path.isfile(UNLUAC_JAR):
        print(f"Błąd: brak pliku unluac.jar pod ścieżką: {UNLUAC_JAR}")
        sys.exit(1)

    print(f"[1/2] Odszyfrowywanie i rozpakowywanie: {src_root} -> {dst_root}")
    archives, others = extract_all(src_root, dst_root)
    print(f"      Rozpakowano archiwów 7z: {archives}, pozostałych plików: {others}")

    print("[2/2] Dekompilacja plików LuaQ przez unluac...")
    ok_count, failures = decompile_tree(dst_root)
    print(f"      Zdekompilowano pomyślnie: {ok_count} plików")

    if failures:
        print(f"      Niepowodzenia ({len(failures)}):")
        for path, err in failures[:10]:
            print(f"        - {path}: {err}")


if __name__ == "__main__":
    main()