#!/usr/bin/env python3
"""Run real SimAdmin registration code in a temporary, test-only checkout.

The caller's checkout is not patched. Only an in-memory peer / synthetic SIM
material are used. This does not connect to a modem, operator or live service.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
ASSETS=('simulator.rs','cellular_adapter.rs','wifi_adapter.rs')
HOOKS={
 'backend/src/connectivity/core/mod.rs':('offline_sim','simulator.rs'),
 'backend/src/connectivity/modems/ims/cellular_ims/live.rs':('offline_sim_adapter','cellular_adapter.rs'),
 'backend/src/connectivity/modems/ims/vowifi/live.rs':('offline_sim_adapter','wifi_adapter.rs'),
}

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def sources(root):
    files=list((root/'backend/src').rglob('*.rs'))+[root/'backend/Cargo.toml',root/'backend/Cargo.lock',root/'backend/build.rs',root/'VERSION']
    return {p.relative_to(root).as_posix():sha(p) for p in sorted(files)}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--consumer-source',type=Path,default=ROOT.parent/'SimAdmin')
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--cargo',default=shutil.which('cargo') or str(Path.home()/'.cargo/bin/cargo'))
    p.add_argument('--target-dir',type=Path)
    args=p.parse_args();source=args.consumer_source.resolve();report=args.report.resolve()
    if not (source/'backend/Cargo.toml').is_file():p.error('select a SimAdmin source checkout')
    raw=report.with_suffix('.raw.json');log=report.with_suffix('.log')
    if any(path.exists() for path in (report,raw,log)):p.error('use a fresh report path; previous evidence is not overwritten')
    report.parent.mkdir(parents=True,exist_ok=True)
    before=sources(source);assets={name:sha(HERE/name) for name in ASSETS}
    with tempfile.TemporaryDirectory(prefix='ims-registration-simulation-') as temporary:
        work=Path(temporary)
        shutil.copytree(source/'backend',work/'backend',ignore=shutil.ignore_patterns('target','.git','__pycache__'))
        shutil.copy2(source/'VERSION',work/'VERSION')
        (work/'offline-registration-sim').mkdir()
        for name in ASSETS:shutil.copy2(HERE/name,work/'offline-registration-sim'/name)
        added=[]
        for name,(module,asset) in HOOKS.items():
            path=work/name;text=path.read_text(encoding='utf-8')
            if f'mod {module} ' not in text:
                text+='\n#[cfg(test)]\npub(crate) mod '+module+' {\n    include!(concat!(env!("CARGO_MANIFEST_DIR"), "/../offline-registration-sim/'+asset+'"));\n}\n'
                path.write_text(text,encoding='utf-8');added.append(name)
            elif '/offline-registration-sim/'+asset not in text:
                raise RuntimeError('a different test module already owns '+module)
        env={**os.environ,'SIMADMIN_DERIVATION_REPORT':str(raw),
             'SIMADMIN_VOWIFI_DEVICE_CHANGES_ALLOWED':'0','SIMADMIN_VOWIFI_LIVE_NETWORK_ALLOWED':'0'}
        env['PATH']=str(Path(args.cargo).parent)+os.pathsep+env.get('PATH','')
        if args.target_dir:env['CARGO_TARGET_DIR']=str(args.target_dir.resolve())
        command=[args.cargo,'test','--manifest-path',str(work/'backend/Cargo.toml'),'--locked','--offline',
                 'offline_derivation_registration_matrix','--','--nocapture','--test-threads=1']
        with log.open('w',encoding='utf-8') as stream:
            result=subprocess.run(command,cwd=work,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=1200)
        if result.returncode or not raw.is_file():raise RuntimeError('simulation failed; inspect '+str(log))
        if not re.search(r'test result: ok\. 1 passed; 0 failed;',log.read_text()):raise RuntimeError('matrix did not execute')
    if before!=sources(source):raise RuntimeError('consumer source changed during simulation')
    if assets!={name:sha(HERE/name) for name in ASSETS}:raise RuntimeError('simulation assets changed')
    data=json.loads(raw.read_text())
    data.update(report_format=1,source_files_sha256=before,harness_files_sha256=assets,
        source_tree_sha256=hashlib.sha256(json.dumps(before,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        log_sha256=sha(log),test_count=1,test_hooks_added_only_in_temporary_copy=added,
        interpretation='Conditional standard-core model only; no live carrier certification. Unknown/stricter configuration must be retained.')
    # Import validation from this producer, not code from the supplied checkout.
    sys.path.insert(0,str(ROOT))
    from simulation_pruning.evidence import validate_evidence
    candidate=report.with_suffix('.validating.json')
    try:
        candidate.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        summary=validate_evidence(candidate,source)
        candidate.replace(report)
    finally:
        if candidate.exists():candidate.unlink()
    print(json.dumps({'report':str(report),**summary},indent=2))

if __name__=='__main__':
    try:main()
    except Exception as error:print('offline simulation failed: '+str(error),file=sys.stderr);raise SystemExit(1)
