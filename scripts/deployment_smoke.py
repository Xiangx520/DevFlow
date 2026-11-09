"""Isolated real Docker deployment smoke. No real GitHub/model/Feishu calls."""
import json
import secrets
import time
import uuid
import docker

def main():
    client=docker.from_env()
    suffix=uuid.uuid4().hex[:12]
    network=client.networks.create("devflow-smoke-"+suffix)
    containers=[]
    password=secrets.token_urlsafe(24)
    token=secrets.token_urlsafe(32)
    def run(image,command=None,**kwargs):
        c=client.containers.create(image,command=command,network=network.name,**kwargs)
        containers.append(c);c.start();return c
    def success(c):
        result=c.wait(timeout=120)
        logs=c.logs().decode(errors="replace")
        if result["StatusCode"]:raise RuntimeError(logs[-6000:])
        return logs
    try:
        pg=run("postgres:16-bookworm",name="devflow-smoke-db-"+suffix,
               environment={"POSTGRES_DB":"devflow_smoke","POSTGRES_USER":"devflow","POSTGRES_PASSWORD":password})
        for _ in range(120):
            if pg.exec_run(["pg_isready","-U","devflow","-d","devflow_smoke"]).exit_code==0:break
            time.sleep(.5)
        else:raise RuntimeError("PostgreSQL not ready")
        env={"DEVFLOW_DATABASE_URL":f"postgresql+psycopg://devflow:{password}@{pg.name}:5432/devflow_smoke",
             "DEVFLOW_API_TOKEN":token,"DEVFLOW_GITHUB_WEBHOOK_SECRET":secrets.token_urlsafe(32)}
        success(run("devflow-backend:local",["alembic","upgrade","head"],environment=env))
        api=run("devflow-backend:local",environment=env)
        code=r"""
import httpx,os,time
for _ in range(120):
 try:
  if httpx.get("http://127.0.0.1:8000/health/ready").status_code==200:break
 except httpx.HTTPError:pass
 time.sleep(.5)
else:raise RuntimeError("API not ready")
with httpx.Client(base_url="http://127.0.0.1:8000",headers={"Authorization":"Bearer "+os.environ["DEVFLOW_API_TOKEN"]}) as c:
 assert httpx.get("http://127.0.0.1:8000/api/v1/projects").status_code==401
 p=c.post("/api/v1/projects",json={"repository":"smoke/isolated","policy":{"allowed_actors":["owner"]},
     "config":{"verify_commands":["python3 --version"],"notify_feishu":False}})
 assert p.status_code==201,p.text
 project=p.json()["id"]
 payload={"project_id":project,"title":"Smoke task","goal":"Inspect only","acceptance":["record result"],"scope":"analysis"}
 a=c.post("/api/v1/tasks",json=payload,headers={"Idempotency-Key":"smoke"})
 b=c.post("/api/v1/tasks",json=payload,headers={"Idempotency-Key":"smoke"})
 assert a.status_code==201 and a.json()["id"]==b.json()["id"]
 assert c.get("/api/v1/tasks/"+a.json()["id"]).json()["status"]=="queued"
 assert c.get("/api/v1/tasks/"+a.json()["id"]+"/events").json()
 assert c.get("/openapi.json").status_code==200
print("migration, auth, project/task persistence, idempotency, events and OpenAPI passed")
"""
        result=api.exec_run(["python","-c",code])
        if result.exit_code:raise RuntimeError(result.output.decode(errors="replace"))
        print(result.output.decode().strip())
        worker_code="""import asyncio,docker
from devflow.watchdog_main import liveness
print('Docker daemon:',docker.from_env().version()['Version'])
asyncio.run(liveness())
print('independent watchdog liveness passed')"""
        print(success(run("devflow-backend:local",["python","-c",worker_code],environment=env,
                          volumes={"/var/run/docker.sock":{"bind":"/var/run/docker.sock","mode":"rw"}})).strip())
        result=success(run("devflow-runner:local",["python3","-c",
            "import os; assert os.getuid()==1000; assert not os.path.exists('/var/run/docker.sock'); print('runner non-root and no daemon socket passed')"],
            entrypoint=[],user="1000:1000",cap_drop=["ALL"],security_opt=["no-new-privileges:true"]))
        print(result.strip())
        print("Deployment smoke passed. No external service writes or model charges.")
    finally:
        for c in reversed(containers):
            try:c.remove(force=True,v=True)
            except docker.errors.NotFound:pass
        network.remove()
if __name__=="__main__":main()
