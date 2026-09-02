# aisgd -- AISG v2.0 RET control over HTTP/WebSocket, plus the aisgctl CLI.
#
# Build (binary build, context is this directory):
#   oc -n aisg start-build aisgctl --from-dir=. -F
FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0
COPY server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /opt/aisgctl/
RUN chmod 0755 /opt/aisgctl/aisgctl /opt/aisgctl/preflight.py \
 && chgrp -R 0 /opt/aisgctl && chmod -R g=u /opt/aisgctl

# gid 18 = dialout; the device node the plugin injects is root:dialout
USER 1001
WORKDIR /opt/aisgctl
ENV PATH=/opt/aisgctl:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aisgctl
EXPOSE 8080
CMD ["python3", "-m", "server.server"]
