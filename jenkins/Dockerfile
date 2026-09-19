# Jenkins controller with Node 20 + Playwright baked in, so pipeline jobs can run
# real E2E automation on the built-in node (no docker-in-docker workspace issues).
FROM jenkins/jenkins:lts-jdk17

USER root

# Node 20 + git
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg git ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Playwright browsers + OS deps, in a shared location the jenkins user can read.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN npx -y playwright@1.55.0 install --with-deps chromium \
 && chmod -R 777 /ms-playwright

USER jenkins

# Plugins (Configuration-as-Code seeds the admin user, credentials, and the job).
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli -f /usr/share/jenkins/ref/plugins.txt

COPY casc.yaml /usr/share/jenkins/casc.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/casc.yaml
# Skip the setup wizard; JCasC owns configuration.
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Dpermissive-script-security.enabled=true"
