## Run your first playbook
1. In the **Configurations** view, make sure a saved Configuration exists (create it in the platform dashboard: Jira connection, app URL, source/destination repos, Jenkins).
2. Click **▶ Run Playbook…** → choose **Functional** → pick the Configuration → enter a **Jira ticket key** (e.g. `QA-1`).
3. Watch the status bar; when it finishes, **Open results** shows the *Ticket → Production* journey: cases stored in Jira, grounded Playwright specs, local self-heal, Jenkins, PR, and the results posted back to the ticket.

Other playbooks: **Security** (SAST · SCA/CVE · secrets · IaC → SARIF), **Performance** (open-model k6 + Web Vitals + per-pod CPU/mem), **Go Code Coverage** (LCOV/Cobertura + a threshold gate — then *Show Go coverage overlay* paints it in your gutter).
