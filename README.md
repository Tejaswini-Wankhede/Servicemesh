# ServiceMesh

### A Failure-Aware After-Sales Service Orchestration Platform

**Connecting customers, OEMs, warranty providers, authorized service centers, repair technicians, and parts suppliers through a coordinated service workflow.**

[View Source Code](https://github.com/Tejaswini-Wankhede/Servicemesh)

---

## 1. Overview

ServiceMesh is a student-developed prototype designed to address coordination and communication challenges in consumer-electronics after-sales service.

A typical repair request may involve multiple independent organizations. Customers can experience delays when warranty verification, service-center availability, technician assignment, spare-parts procurement, and repair confirmation are handled across disconnected systems.

ServiceMesh explores how these interactions can be coordinated through a stateful, failure-aware orchestration platform.

The system maintains service-request state, records workflow history, handles failures, and supports escalation and recovery across simulated organizational boundaries.

**Project stage:** Working prototype with simulated organizations. Seeking industry mentorship, technical collaboration, and opportunities for controlled pilot evaluation.

---

## 2. The Problem

After-sales service workflows can involve several parties:

* Customers reporting product issues.
* OEMs and warranty providers verifying coverage.
* Service centers assigning technicians.
* Repair personnel diagnosing and resolving issues.
* Parts suppliers fulfilling component requirements.

When communication between these parties is fragmented, customers may have limited visibility into their requests, while service providers face coordination and follow-up challenges.

ServiceMesh investigates how these workflows can be made more traceable, coordinated, and resilient to operational failures.

---

## 3. Proposed Solution

ServiceMesh provides a centralized orchestration layer that coordinates interactions among simulated organizations while maintaining a persistent record of each service transaction.

The prototype is designed to:

* Track service requests and workflow progress.
* Coordinate warranty, service-provider, and parts-related operations.
* Handle timeouts, failed operations, retries, and fallback paths.
* Maintain workflow history and audit records.
* Support service-center and supplier interactions through dedicated portals.
* Provide customers with persisted in-app notifications and service updates.

ServiceMesh acts as a **coordination and orchestration layer** rather than replacing the participating organizations or their internal systems.

---

## 4. Current Prototype

The repository includes the following implemented components:

| Component                | Purpose                                                   |
| ------------------------ | --------------------------------------------------------- |
| Customer React UI        | Customer-facing service-request experience                |
| FastAPI backend          | API and orchestration services                            |
| PostgreSQL               | Persistent workflow state and history                     |
| Organization simulators  | Simulated OEM, marketplace, warranty, and partner systems |
| Service-center portal    | Service-provider operations                               |
| Supplier portal          | Parts-related workflow operations                         |
| Admin dashboard          | Operational visibility and failure simulation             |
| ML-assisted risk scoring | Additional input for provider-risk assessment             |

The core workflow uses a stateful business process with retry, fallback, compensation, and escalation mechanisms.

The current prototype uses simulated organizations. Production integrations would require authorization and API access from participating organizations.

---

## 5. Technology Stack

* **Frontend:** React
* **Backend:** Python, FastAPI
* **Database:** PostgreSQL
* **Deployment:** Docker, Docker Compose
* **Event Transport:** Optional Redpanda integration
* **Machine Learning:** ML-assisted provider risk scoring

Refer to the repository's setup and deployment documentation for exact installation and configuration requirements.

---

## 6. Demonstration Workflow

A representative demonstration can cover:

1. A customer raises an electronics service request.
2. Product and purchase information is verified.
3. Warranty eligibility is checked.
4. An authorized service center is selected.
5. The component requiring service is identified.
6. Compatibility and replacement-part requirements are checked.
7. The required part is reserved through the simulated supplier.
8. The repair workflow is coordinated through the service center.
9. Evidence and service completion are verified.
10. The request is closed or escalated if recovery is unsuccessful.

A failure can also be injected into the workflow to demonstrate retry, fallback, recovery, or escalation.

All demonstrations should be understood as prototype behavior in a simulated environment.

---

# 7. Screenshots

The following screenshots document the major user-facing and backend components of the ServiceMesh prototype.

### 01 — Home / Dashboard

The main ServiceMesh interface showing the application name, navigation, and primary dashboard.

![Home / Dashboard](assets/01-home-dashboard.png)

---

### 02 — Customer Page

The customer-facing page where a customer can access their service requests and initiate an after-sales service workflow.

![Customer Page](assets/02-customer-page.png)

---

### 03 — Service Request Form

The request creation interface containing relevant information such as product details, warranty information, and issue description.

![Service Request Form](assets/03-service-request-form.png)

---

### 04 — Service Request Tracking

Shows the status and progression of a service request, including workflow steps, transaction state, and request history.

![Service Request Tracking](assets/04-service-request-tracking.png)

---

### 05 — Service Center / Technician Page

Shows the operational interface used for service coordination, request assignment, diagnosis, repair-related activities, or technician operations.

![Service Center / Technician](assets/05-service-center-technician.png)

---

### 06 — Database / Backend

Shows the backend and persistent workflow infrastructure, such as FastAPI endpoints, PostgreSQL data, database records, or workflow execution logs.

![Database / Backend](assets/06-database-backend.png)

> **Note:** Backend screenshots should not expose passwords, API keys, tokens, personal information, database credentials, or other sensitive configuration.

---

## 8. Failure Simulation and Recovery

ServiceMesh includes a failure-simulation mechanism to demonstrate how the orchestration layer behaves when a participating provider becomes temporarily unavailable or experiences an operational failure.

For example:

```text
Service Request
      ↓
Warranty Validation
      ↓
Service Centre Selection
      ↓
Parts Supplier
      ↓
     FAILURE
      ↓
Failure Classification
      ↓
Retry / Recovery / Fallback
      ↓
Resume from Persisted State
```

The failure-simulation interface allows controlled failures to be introduced into the simulated provider environment.

This allows the prototype to demonstrate failure-aware workflow behavior without requiring real external organizations.

---

## 9. Industry Collaboration

We are seeking collaboration with companies operating in:

* After-sales service
* Warranty management
* Field-service operations
* Consumer electronics
* Service-center management
* Parts and spare-component supply chains

Potential areas of collaboration include:

* Industry mentorship and workflow validation.
* Technical feedback and architecture review.
* Controlled pilot testing with suitable, non-sensitive workflows.
* Technical resources or student-project sponsorship.
* Guidance on practical integration and deployment requirements.

The current prototype uses simulated organizations. Real-world deployment would require participating organizations to authorize access to their systems and provide suitable integration interfaces.

---

## 10. Project Links

* GitHub repository: https://github.com/Tejaswini-Wankhede/Servicemesh
* Setup instructions: `SETUP.md`
* Deployment instructions: `DEPLOYMENT.md`

**Project Contact:** Parth Sawant
**Institution:** Vishwakarma Institute of Technology, Pune
**Email:** [ADD PROJECT EMAIL HERE]

---

## 11. Repository Structure

```text
ServiceMesh/
│
├── assets/
│   ├── 01-home-dashboard.png
│   ├── 02-customer-page.png
│   ├── 03-service-request-form.png
│   ├── 04-service-request-tracking.png
│   ├── 05-service-center-technician.png
│   └── 06-database-backend.png
│
├── frontend/
├── servicemesh/
├── providers/
├── migrations/
├── tests/
├── deploy/
├── scripts/
│
├── README.md
├── SETUP.md
└── DEPLOYMENT.md
```

---

## 12. Project Status

**Status: Working Prototype**

ServiceMesh currently demonstrates cross-organization service orchestration using simulated providers.

The prototype focuses on:

* Stateful workflow orchestration
* Provider interaction through adapters
* Persistent transaction state
* Failure simulation
* Retry and recovery
* Fallback and escalation
* Service-request tracking
* Operational dashboards

Real-world provider integrations, production deployment, and controlled industry validation remain future development and collaboration opportunities.

