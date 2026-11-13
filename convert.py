"""
Download files from a Google Drive folder, convert them, and upload the
results to another Drive folder. Used by .github/workflows/convert.yml

Folders can be given as a path from My Drive (e.g. "gdrive-connect/convert-here")
or as a raw folder ID prefixed with "id:" (e.g. "id:1AbC...").
"""
import argparse
import os
import re
import shutil
import subprocess
import tempfile

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from PIL import Image

SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------- Drive helpers ----------
def get_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _esc(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


def resolve_folder(svc, spec):
    """Turn 'a/b/c' (from My Drive) or 'id:<ID>' into a folder ID."""
    spec = spec.strip()
    if spec.lower().startswith("id:"):
        return spec[3:].strip()

    parts = [p for p in spec.replace("\\", "/").split("/") if p.strip()]
    if parts and parts[0].lower() == "my drive":
        parts = parts[1:]
    if not parts:
        return "root"

    parent, walked = "root", []
    for name in parts:
        walked.append(name)
        resp = svc.files().list(
            q=(f"'{parent}' in parents and name='{_esc(name)}' "
               f"and mimeType='{FOLDER_MIME}' and trashed=false"),
            fields="files(id, name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()["files"]
        where = "/".join(walked)
        if not resp:
            raise SystemExit(f"Folder not found: '{where}' (checked from My Drive). "
                             "Check the spelling and capitalisation.")
        if len(resp) > 1:
            raise SystemExit(f"More than one folder named '{where}' exists. "
                             "Rename or remove the duplicate, or use 'id:<folder ID>'.")
        parent = resp[0]["id"]
    return parent


def list_files(svc, folder_id):
    files, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files += resp["files"]
        token = resp.get("nextPageToken")
        if not token:
            return files


def download(svc, file_id, path):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


def trash(svc, file_id):
    """Move a file to Drive's Trash (recoverable for 30 days)."""
    svc.files().update(
        fileId=file_id, body={"trashed": True}, supportsAllDrives=True
    ).execute()


def upload(svc, path, folder_id):
    media = MediaFileUpload(path, resumable=True)
    return svc.files().create(
        body={"name": os.path.basename(path), "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()


# ---------- Converters: each takes (src, out_dir) and returns the new file path ----------
def _stem(src):
    return os.path.splitext(os.path.basename(src))[0]


def libreoffice(fmt):
    def run(src, out_dir):
        subprocess.run(
            ["soffice", "--headless", "--convert-to", fmt, "--outdir", out_dir, src],
            check=True, timeout=600,
        )
        return os.path.join(out_dir, f"{_stem(src)}.{fmt}")
    return run


def pillow(fmt, ext):
    def run(src, out_dir):
        out = os.path.join(out_dir, f"{_stem(src)}.{ext}")
        img = Image.open(src)
        if fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")  # JPEG has no transparency
        img.save(out, fmt)
        return out
    return run


def ffmpeg(ext, extra=None):
    def run(src, out_dir):
        out = os.path.join(out_dir, f"{_stem(src)}.{ext}")
        cmd = ["ffmpeg", "-y", "-i", src] + (extra or []) + [out]
        subprocess.run(cmd, check=True, timeout=3600)
        return out
    return run


def csv_to_xlsx(src, out_dir):
    out = os.path.join(out_dir, f"{_stem(src)}.xlsx")
    pd.read_csv(src).to_excel(out, index=False)
    return out


def xlsx_to_csv(src, out_dir):
    out = os.path.join(out_dir, f"{_stem(src)}.csv")
    pd.read_excel(src).to_csv(out, index=False)
    return out


# mode -> (accepted input extensions, converter)
MODES = {
    "png-to-jpg":   ((".png",), pillow("JPEG", "jpg")),
    "jpg-to-png":   ((".jpg", ".jpeg"), pillow("PNG", "png")),
    "webp-to-jpg":  ((".webp",), pillow("JPEG", "jpg")),
    "docx-to-pdf":  ((".docx", ".doc"), libreoffice("pdf")),
    "pptx-to-pdf":  ((".pptx", ".ppt"), libreoffice("pdf")),
    "xlsx-to-pdf":  ((".xlsx", ".xls"), libreoffice("pdf")),
    "csv-to-xlsx":  ((".csv",), csv_to_xlsx),
    "xlsx-to-csv":  ((".xlsx",), xlsx_to_csv),
    "mp4-to-mp3":   ((".mp4", ".mov", ".mkv"), ffmpeg("mp3", ["-vn", "-q:a", "2"])),
    "wav-to-mp3":   ((".wav",), ffmpeg("mp3", ["-q:a", "2"])),
    "mp3-to-wav":   ((".mp3",), ffmpeg("wav")),
    "mov-to-mp4":   ((".mov",), ffmpeg("mp4", ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"])),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="gdrive-connect/convert-here")
    p.add_argument("--target", default="gdrive-connect/convert-done")
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--delete-originals", default="true",
                   help="true/false: trash source files after a successful conversion")
    p.add_argument("--job-id", default="",
                   help="only convert files named '<job-id>__*' (used by the web page)")
    args = p.parse_args()
    delete_originals = args.delete_originals.strip().lower() == "true"
    job_id = args.job_id.strip()
    if job_id and not re.fullmatch(r"[A-Za-z0-9-]{8,64}", job_id):
        raise SystemExit("Invalid job id: use 8-64 letters, digits or dashes.")
    job_prefix = f"{job_id}__" if job_id else ""

    exts, converter = MODES[args.mode]
    svc = get_service()

    source_id = resolve_folder(svc, args.source)
    target_id = resolve_folder(svc, args.target)
    print(f"Source: {args.source} -> {source_id}")
    print(f"Target: {args.target} -> {target_id}")

    files = [
        f for f in list_files(svc, source_id)
        if not f["mimeType"].startswith(GOOGLE_NATIVE_PREFIX)
        and f["name"].lower().endswith(exts)
        and f["name"].startswith(job_prefix)
    ]
    scope = f" for job {job_id}" if job_id else ""
    print(f"Found {len(files)} matching file(s) for '{args.mode}'{scope}")
    if job_id and not files:
        raise SystemExit(f"No file for job {job_id} was found in the source folder.")

    # Output names inherit the '<job-id>__' prefix from the input name, so the
    # web backend can find the result in the target folder by that prefix.

    ok, failed = 0, []
    for f in files:
        work = tempfile.mkdtemp()
        try:
            src = os.path.join(work, f["name"])
            out_dir = os.path.join(work, "out")
            os.makedirs(out_dir)
            print(f"-> {f['name']}")
            download(svc, f["id"], src)
            out = converter(src, out_dir)
            upload(svc, out, target_id)  # must succeed before the original is touched
            if delete_originals and source_id != target_id:
                trash(svc, f["id"])
                print("   original moved to Trash")
            ok += 1
        except Exception as e:
            print(f"   FAILED: {e}")
            failed.append(f["name"])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print(f"\nDone. {ok} converted, {len(failed)} failed.")
    if failed:
        print("Failed files:", ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()