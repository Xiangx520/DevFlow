"""Explicit opt-in real-service smoke test; owner supplies an existing test project."""
import argparse
import os
import httpx

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--url",default="http://localhost:8000")
    p.add_argument("--project-id")
    p.add_argument("--goal")
    p.add_argument("--allow-github-write",action="store_true")
    args=p.parse_args()
    token=os.environ.get("DEVFLOW_API_TOKEN","")
    with httpx.Client(base_url=args.url,headers={"Authorization":"Bearer "+token},timeout=30) as c:
        response=c.get("/health/ready");response.raise_for_status();print(response.json())
        if not args.allow_github_write:
            print("Read-only check complete. No task or external write requested.")
            return
        if not args.project_id or not args.goal:p.error("--project-id and --goal are required")
        import uuid
        response=c.post("/api/v1/tasks",headers={"Idempotency-Key":"live-"+str(uuid.uuid4())},
                        json={"project_id":args.project_id,"title":"Explicit live smoke task",
                              "goal":args.goal,"acceptance":["Reviewable implementation with recorded verification"]})
        response.raise_for_status()
        print("Submitted task:",response.json()["id"])
        print("Poll /api/v1/tasks/<id> and events. Model charges and GitHub PR writes are now possible.")
if __name__=="__main__":main()
