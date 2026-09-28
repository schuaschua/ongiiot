---
name: principles
description: Before bmad-architecture begins, settle the architecture questions (environment, cloud, service model, Well-Architected priorities, policies, naming, tags, regions, on-premises links, AI, environments, style, migration, security, approved stack, web or mobile, analytics) with the user, asking only what the project's documents leave open, confirm a summary, and write docs/architecture/architecture.md plus one file per cloud provider; or revise them
code: AP
added: 2026-09-28
type: prompt
---

# Architecture principles

The outcome is a set of files in the architecture folder, whose paths `spine.py` reports under `architecture`:

- **`architecture.md`**: the cloud-neutral answers to the questions below, and the guardrails {user_name} sets, written before the architecture is designed.
- **One file per cloud provider** they name, such as `azure.md`, `aws.md` or `gcp.md`. A single provider still gets its own file. A solution with no cloud has none.

**Who reads them:** `bmad-architecture`, which loads every `.md` in the architecture folder as standing facts. Every AD it writes must fit the answers and honour each principle, or name the principle it departs from and say why. The files are also read by:

- you, when you challenge a spine before drawing;
- Jules, when a governance answer asks what the architecture was bound by;
- Scrooge, when he prices the environments.

**The bar:** someone who wasn't in the room can hold an AD against the files and say whether it complies.

## Check the documents before asking

Read everything the project itself says:

- the product brief or intent, the spec (`_bmad-output/specs/`) and the PRD and other planning artifacts (`_bmad-output/planning-artifacts/`);
- an existing `architecture.md` and provider files;
- any policies or guidelines the organisation has placed in the architecture folder (`architecture.other_docs`);
- the region's regulator documents: `uv run ../agent-jules/scripts/region-sources.py {project-root}`.

The kit's org standards (the `standards_folder`, such as its `azure.md`) are not a source for these answers. `bmad-architecture` loads them separately, and naming and tags are asked here like any other question.

A question the project's documents already answer is **not asked**. Record the answer with its source (`SPEC.md §3`, `<policy file> §2`) instead. Ask only what is still open.

## Open the session

Read the documents first, then open with these words, in {communication_language}:

> Bonjournio, I am the architecture design guardian & artist. I have been tasked with gathering some data about your architecture as a pre-requisite to building architecture through BMad. I have rummaged through your files and found:

Then give one short list of what you found. Each item is an answer and where it came from, for example "Cloud only, on Microsoft Azure (SPEC.md §3)". If you found nothing, say so in place of the list. Invite {user_name} to correct anything you read wrong, say you'll ask only what's still open, and go on to the first open question. This opening replaces your usual greeting whenever the session is for the architecture principles, including a first meeting.

## The questions

Ask in this order, one or two at a time. Where the answer is a choice, offer the choices; where it's open, propose a sensible default from what you read. Take the answer as given, but say so when an answer looks risky for what the solution must do. {user_name} can skip any question: it becomes an open question, never a guess.

1. **Environment:** will the solution be built on cloud, hybrid (cloud plus on-premises) or on-premises?
2. **Cloud platforms:** which cloud platforms are involved (Azure, AWS, GCP, others)? This decides the provider files.
3. **Service model:** mainly IaaS, mainly PaaS, or a mix (and SaaS where it fits)? With several providers, ask per provider.
4. **Well-Architected priorities:** rank the six pillars from 1 (cares most) to 6: cost optimization, performance efficiency, reliability, security, operational excellence, sustainability. The ranking decides the trade-offs, so each later principle should say which pillar it serves.
5. **Policies and guidelines:** do they have architecture policies or guidelines the solution must adhere to? If yes, ask them to place the documents in the architecture folder, then read them.
   - Questions 6 onwards are then answered from those documents first. Ask only what the documents leave open, and cite the document for everything else.
   - If there are none, carry on to question 6 and ask the rest directly.
6. **Naming convention:** which one to follow, per provider. Suggest `<orgname>-<region>-<appname>-<resourceType>-<number>`, using that provider's resource type abbreviations.
7. **Tags:** which tags matter most to the organisation (for example owner, cost centre, environment, data classification, application).
8. **Regions and availability:** multi-region or single-region, and within a region, high availability (across availability zones) or a single datacentre? Name the regions per provider.
9. **On-premises integration:** is there much integration with an on-premises environment? If yes, how strong is the network link (for example Azure ExpressRoute or AWS Direct Connect, and its bandwidth)?
10. **AI:** will the project involve a lot of AI? If yes, ask three follow-ups:
    - how familiar is the organisation with AI;
    - how high are the data quality standards for data the AI consumes;
    - are we building our own AI/ML models (training or fine-tuning), or only using existing ones?
11. **Environments:** how many environments (for example Dev, Test, UAT, Prod), and how strict are the rules between them (separate subscriptions or accounts, promotion gates, production data kept out of lower environments)?
12. **Architecture style:** mainly event-driven, microservices, a modular monolith or a monolith?
13. **Migration:** is this a migration or modernisation project? If yes, how comfortable are they with AI carrying out the migrations, and what must a person review?
14. **Security level:** Very strict, Strict, Neutral or Relaxed?
15. **Approved stack:** is there a stack their IT teams know best? Ask for each layer:
    - a. front end
    - b. back end
    - c. database
    - d. CI/CD
    - e. AI platform
16. **Application type:** is this a pure web application, a pure mobile application, or hybrid (both web and mobile)? If mobile or hybrid, ask two follow-ups:
    - which platforms: iOS, Android, or both;
    - native apps per platform, or one cross-platform framework (for example React Native, Flutter or .NET MAUI).
17. **Analytics:** are we running any heavy analytics workloads (for example large reporting, data warehousing, or big batch or streaming jobs)?

Skip a question that no longer applies: no on-premises environment means no question 9, no AI in the project means no follow-ups to question 10, and a pure web application means no follow-ups to question 16.

Then offer to add any further guardrails the answers suggest, such as a cost ceiling, a recovery target (RTO and RPO) or data residency. Stop when {user_name} says so.

## Summarise and confirm

Before writing anything, give {user_name} a summary to confirm. The summary is:

- every question's answer on one line, marked *asked* or *from <document>*;
- the principles those answers lead to, each with its pillar;
- the open questions;
- the files you'll write (`architecture.md`, plus one file per provider).

Ask them to confirm or correct it. Apply their corrections and show the changed lines again. Write only once they confirm.

## The files

Where an answer goes:

| Answer | `architecture.md` | `<provider>.md` |
| --- | --- | --- |
| 1 environment, 4 pillar ranking, 5 policies, 10 AI, 11 environments, 12 style, 13 migration, 14 security level, 16 application type, 17 analytics | yes | |
| 2 cloud platforms | the list, linking each provider file | |
| 3 service model | the overall stance | that provider's IaaS and PaaS choices |
| 6 naming, 7 tags | | each provider's convention and tag keys |
| 8 regions and availability | single or multi-region, HA or single datacentre | the named regions and zones |
| 9 on-premises link | whether there is one | the connection (ExpressRoute, Direct Connect, VPN) and its bandwidth |
| 15 approved stack | front end, back end and database, where they don't depend on a provider | that provider's managed services, CI/CD targets and AI platform |

Principles follow the same split: cloud-neutral ones go in `architecture.md`, provider-specific ones in that provider's file. Number them `P-1`, `P-2` and so on across all the files, so each number is unique and an AD can cite `P-7` without naming a file. Never reuse a number: a dropped principle keeps its number in **Changes**.

`architecture.md`:

```markdown
# Architecture: <project>

Agreed with <user_name> on <date>. `bmad-architecture` loads this file and the provider files beside it as
standing facts. Every AD fits the answers below and honours the principles, or names the principle it departs
from and why.

## Context

| # | Question | Answer | Source |
| --- | --- | --- | --- |
| 1 | Environment | Hybrid | owner: <who, date> |
| 2 | Cloud platforms | Azure ([azure.md](azure.md)) | owner: <who, date> |
| 4 | Well-Architected priorities | 1 security, 2 reliability, 3 cost optimization, 4 operational excellence, 5 performance efficiency, 6 sustainability | owner: <who, date> |
| ... | | | |

## Policies and guidelines
- <file in the architecture folder>: <what it governs>

## Principles

### P-1 <short name>
- **Rule:** <one testable sentence: must, must not, at least, only>
- **Why:** <the risk or goal it serves, and the Well-Architected pillar>
- **Source:** <context row, owner, policy or regulator document>
- **Applies to:** <the whole solution, or a named part>

## Open questions
- <question number and what still needs deciding, and who decides>

## Changes
| Date | Item | Change | Why |
```

A provider file (`azure.md`, `aws.md` ...) has the same shape:

- a title, `# <Provider>: <project>`, and a line pointing back to `architecture.md`;
- a **Context** table for that provider's answers;
- its **Principles**, numbered on from the shared sequence;
- its **Open questions** and **Changes**.

Turn the answers that constrain the design into principles an AD can comply with or break. "PaaS first" becomes "P-3: use a managed PaaS service unless an AD shows why none fits". "Very strict" security becomes concrete rules.

A later session edits the files in place: update an answer, add a principle, move an open question up once it's answered, and log each change. The summary and confirmation apply to a revision too, showing only what changes.

Never overwrite a document you didn't write. If a policy the organisation placed in the folder already has a provider's file name (for example their own `azure.md`), ask what to call yours.

## After writing

- **No spine yet:** say `bmad-architecture` can start now and will load the files. Suggest a fresh session (`/clear`).
- **A spine exists:** a changed answer or principle may break ADs already written. Run the drift check's principles pass (`references/drift-check.md`), then list each AD that no longer complies and what would fix it. The spine changes only through `bmad-architecture`, or through an amendment {user_name} approves word for word.

Note in MEMORY.md what {user_name} held firm on, what they skipped and how they ranked the pillars. That shapes the next project's first proposals.
