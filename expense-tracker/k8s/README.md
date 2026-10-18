# Kubernetes starter manifests

These manifests are for local learning with Minikube or kind. They use a single replica and SQLite on a PersistentVolumeClaim so the database survives pod recreation.

## Before deploying

1. Build the image locally:
   `docker compose --env-file .env.docker -f compose.yaml build`
2. For Minikube, make the image available to the cluster using `minikube image load fleetflow:local`. For kind, use `kind load docker-image fleetflow:local`.
3. Create a Kubernetes Secret for the Flask key. Generate a unique key with:
   `python -c "import secrets; print(secrets.token_hex(32))"`
4. Review the namespace, storage class, resource requests, and ingress settings for your cluster.

Apply from the repository root:
```bash
kubectl apply -f expense-tracker/k8s/
kubectl -n fleetflow get pods,svc,pvc
kubectl -n fleetflow port-forward svc/fleetflow 5001:5001
```
Then open http://127.0.0.1:5001.

## Important

- Do not use SQLite with multiple replicas. Keep replicas at 1 while using SQLite.
- For multiple replicas or a production deployment, migrate to PostgreSQL and use a managed database or properly operated database service.
- The Flask secret is supplied through a Kubernetes Secret; do not commit the actual secret.
- Add HTTPS, ingress, backups, monitoring, SMTP secrets, and a production deployment review before exposing this publicly.
