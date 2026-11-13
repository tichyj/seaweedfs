FROM python:3.12-alpine

# curl / dig are here so the boundary evidence can be reproduced by hand
# ("does this name even resolve from inside the client network?").
RUN apk add --no-cache curl bind-tools \
 && pip install --no-cache-dir "boto3>=1.34,<2"

WORKDIR /app
COPY s3_workload.py net_probe.py /app/

CMD ["sleep", "infinity"]
