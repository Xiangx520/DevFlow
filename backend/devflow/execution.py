"""Docker execution adapter. Only the worker has access to the Docker daemon."""
import asyncio
import json
import os
from pathlib import Path
import re
import time
from .config import settings

class DockerExecution:
    def __init__(self):
        import docker
        self.client = docker.from_env()

    def repository_url(self, project):
        return f"https://github.com/{project.repository}.git"

    def volume(self, task_id): return "devflow-task-" + task_id

    def helper(self, task_id, command, *, environment=None, timeout=600):
        container = self.client.containers.create(settings().runner_image, command=command,
            entrypoint=[], user="1000:1000", working_dir="/workspace/repo",
            labels={"devflow.task": task_id, "devflow.kind": "helper"},
            environment={"HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                         **(environment or {})},
            volumes={self.volume(task_id): {"bind": "/workspace", "mode": "rw"}},
            network=settings().runner_network, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], mem_limit="2g", pids_limit=256,
            tmpfs={"/tmp": "rw,nosuid,size=256m"})
        try:
            container.start()
            result = container.wait(timeout=timeout)
            output = container.logs().decode("utf-8", errors="replace")
            if result["StatusCode"]: raise RuntimeError(f"helper exited {result['StatusCode']}: {output[-4000:]}")
            return output[-4 * 1024 * 1024:]
        finally:
            container.remove(force=True)

    def prepare(self, task, project):
        self.client.volumes.create(self.volume(task.id), labels={"devflow.task": task.id})
        # Workdir must exist before helper start; image ships /workspace/repo owned by uid 1000.
        script = """
import os, subprocess
from pathlib import Path
repo=Path('/workspace/repo')
if not (repo/'.git').exists():
 subprocess.run(['git','clone','--no-hardlinks',os.environ['REPO_URL'],str(repo)],check=True)
 subprocess.run(['git','-C',str(repo),'checkout','-b',os.environ['TASK_BRANCH'],'refs/remotes/origin/'+os.environ['DEFAULT_BRANCH']],check=True)
else:
 if (repo/'.git').is_symlink() or not (repo/'.git').is_dir(): raise RuntimeError('invalid Git metadata')
 (repo/'.git/config').write_text('[core]\\nrepositoryformatversion = 0\\nfilemode = true\\nbare = false\\n')
 branch=subprocess.check_output(['git','-C',str(repo),'-c','core.hooksPath=/dev/null','branch','--show-current'],text=True).strip()
 if branch!=os.environ['TASK_BRANCH']: raise RuntimeError('workspace branch mismatch; inspect before recovery')
 if os.environ.get('HAS_PR') and os.environ.get('EXPECTED_SHA'):
  local=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
  if local!=os.environ['EXPECTED_SHA']:
   if subprocess.check_output(['git','status','--porcelain'],text=True): raise RuntimeError('remote changed while local work is uncommitted; inspect before recovery')
   subprocess.run(['git','-c','core.hooksPath=/dev/null','fetch',os.environ['REPO_URL'],os.environ['TASK_BRANCH']],check=True)
   subprocess.run(['git','-c','core.hooksPath=/dev/null','merge','--ff-only','FETCH_HEAD'],check=True)
base=os.environ.get('BASE_SHA') or subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
print('DEVFLOW_BASE='+base)
"""
        output = self.helper(task.id, ["python3", "-c", script], environment={
            "REPO_URL": self.repository_url(project), "TASK_BRANCH": task.branch, "HAS_PR": str(task.pr_number or ""),
            "EXPECTED_SHA": task.head_sha or "", "BASE_SHA": task.base_sha or "",
            "DEFAULT_BRANCH": project.default_branch,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + __import__("base64").b64encode(
                ("x-access-token:" + settings().github_token).encode()).decode()})
        return {"base_sha": output.strip().split("DEVFLOW_BASE=")[-1].strip()}

    def start_container(self, task_id):
        return self.client.containers.run(settings().runner_image, ["sleep", "infinity"], entrypoint=[],
            detach=True, user="1000:1000", working_dir="/workspace/repo",
            environment={"HOME": "/tmp"}, read_only=True,
            tmpfs={"/tmp": "rw,nosuid,size=256m"},
            volumes={self.volume(task_id): {"bind": "/workspace", "mode": "rw"}},
            network=settings().runner_network, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="2g", pids_limit=256, labels={"devflow.task": task_id})

    def stop(self, container_id):
        try: self.client.containers.get(container_id).remove(force=True)
        except Exception as exc:
            if exc.__class__.__name__ != "NotFound": raise

    async def attach(self, container_id):
        return await asyncio.create_subprocess_exec("docker", "exec", "-i", container_id,
            "node", "/opt/devflow/pi-service/dist/main.js",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024)

    def verify(self, task, project):
        commands = project.config["verify_commands"]
        # No credential is present while project code or verification commands run.
        script = """
import json,os,subprocess
from pathlib import Path
repo=Path('/workspace/repo')
if (repo/'.git').is_symlink() or not (repo/'.git').is_dir(): raise RuntimeError('invalid Git metadata')
(repo/'.git/config').write_text('[core]\\nrepositoryformatversion = 0\\nfilemode = true\\nbare = false\\n')
args=['git','-c','core.hooksPath=/dev/null','-c','user.name=DevFlow','-c','user.email=devflow@localhost']
subprocess.run(args+['add','--all'],check=True)
changed=subprocess.run(args+['diff','--cached','--quiet']).returncode
if changed: subprocess.run(args+['commit','-m',os.environ['TASK_TITLE'][:200]],check=True)
sha=subprocess.check_output(args+['rev-parse','HEAD'],text=True).strip()
base=os.environ.get('BASE_SHA')
if not base:
 p=subprocess.run(args+['rev-parse','HEAD^'],capture_output=True,text=True)
 base=p.stdout.strip() if p.returncode==0 else sha
diff=subprocess.check_output(args+['diff','--no-ext-diff','--no-textconv',base,'HEAD'],text=True)
changed_files=subprocess.check_output(args+['diff','--name-only',base,'HEAD'],text=True).splitlines()
results=[]
for command in json.loads(os.environ['SETUP_COMMANDS'])+json.loads(os.environ['VERIFY_COMMANDS']):
 p=subprocess.run(['/bin/sh','-lc',command],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=600)
 results.append({'command':command,'exit_code':p.returncode,'output':p.stdout[-8000:]})
 if p.returncode: break
after=subprocess.check_output(args+['status','--porcelain'],text=True)
print('DEVFLOW_VERIFICATION='+json.dumps({'sha':sha,'commands':results,
 'passed':all(x['exit_code']==0 for x in results) and not after,'dirty_after_verification':bool(after),
 'base_sha':base,'changed_files':changed_files,'diff':diff[:24000],'diff_truncated':len(diff)>24000}))
"""
        output = self.helper(task.id, ["python3", "-c", script], environment={
            "TASK_TITLE": task.title, "BASE_SHA": task.base_sha or "", "SETUP_COMMANDS": json.dumps(project.config.get("setup_commands", [])),
            "VERIFY_COMMANDS": json.dumps(commands)},
            timeout=max(1,min(1800,project.config.get("limits",{}).get("task_seconds",7200)-task.active_seconds)))
        line = next((l for l in reversed(output.splitlines()) if l.startswith("DEVFLOW_VERIFICATION=")), None)
        if not line: raise RuntimeError("verification did not produce a host result")
        return json.loads(line.split("=", 1)[1])

    def push(self, task, project):
        # Fresh helper: no worker process or verification subprocess shares the credential.
        script = """
import os,subprocess
from pathlib import Path
repo=Path('/workspace/repo')
if (repo/'.git').is_symlink() or not (repo/'.git').is_dir(): raise RuntimeError('invalid Git metadata')
(repo/'.git/config').write_text('[core]\\nrepositoryformatversion = 0\\nfilemode = true\\nbare = false\\n')
args=['git','-c','core.hooksPath=/dev/null']
sha=subprocess.check_output(args+['rev-parse','HEAD'],text=True).strip()
if sha!=os.environ['EXPECTED_SHA']: raise RuntimeError('workspace changed after verification')
if subprocess.check_output(args+['status','--porcelain'],text=True): raise RuntimeError('dirty workspace')
remote=subprocess.check_output(args+['ls-remote',os.environ['REPO_URL'],'refs/heads/'+os.environ['TASK_BRANCH']],text=True).strip()
if not remote or remote.split()[0]!=sha:
 subprocess.run(args+['push',os.environ['REPO_URL'],'HEAD:refs/heads/'+os.environ['TASK_BRANCH']],check=True)
"""
        return self.helper(task.id, ["python3", "-c", script], environment={
            "EXPECTED_SHA": task.head_sha, "TASK_BRANCH": task.branch,
            "REPO_URL": self.repository_url(project),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + __import__("base64").b64encode(
                ("x-access-token:" + settings().github_token).encode()).decode()})
