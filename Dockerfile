# openaisg -- AISG v2.0 RET control over HTTP/WebSocket, plus the aisgctl CLI.
#
# Build (binary build, context is the repo root):
#   oc -n aisg start-build aisgctl --from-dir=. -F
FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /opt/openaisg/
RUN chmod 0755 /opt/openaisg/cli/aisgctl /opt/openaisg/utils/preflight.py \
 && ln -s /opt/openaisg/cli/aisgctl /usr/local/bin/aisgctl \
 && chgrp -R 0 /opt/openaisg && chmod -R g=u /opt/openaisg

# gid 18 = dialout; the device node the plugin injects is root:dialout
USER 1001
WORKDIR /opt/openaisg
ENV PATH=/opt/openaisg/cli:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/opt/openaisg
EXPOSE 8080
CMD ["python3", "-m", "api.app"]
