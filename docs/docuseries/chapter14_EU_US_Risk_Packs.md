# Chapter 14: EU and US Risk Evidence Packs

Baseline reviewed: 21 October 2026. This chapter specifies product architecture for an agnostic verification lab, not a legal opinion about a particular business. Examples are synthetic. No example authorizes deployment, certification, or consequential decisions.

## 1. The product contract

The lab should answer three separate questions:

1. What technical behavior did the selected tests observe?
2. What evidence exists for a particular requirement, and what is missing?
3. What applicability or legal conclusions has an authorized human reviewer recorded?

Never collapse these into a single “EU compliant” or “US certified” badge. Passing a benchmark does not establish legal conformity; possessing a policy does not establish that it works. The same technical evidence may support multiple packs, but a crosswalk is not a claim that their legal requirements are equivalent.

Start with a shared evidence engine and separate jurisdiction packs. Attach each result to a specific system release, deployment context, pack version, assessment date, and evidence snapshot. A repository alone usually cannot establish intended deployment, actor roles, affected populations, or territorial scope.

## 2. Source hierarchy and verification limits

Store sources as first-class records. Prefer enacted legal text and official consolidated versions, then regulator guidance and official implementation pages. Distinguish legislation, regulations, agency policy, voluntary frameworks, draft guidance, and explanatory summaries.

For this chapter, direct retrieval of the EUR-Lex consolidated PDF and ELI pages encountered a JavaScript robot check. The European Commission AI Act Service Desk was readable and explicitly identifies its article text as based on the 27 July 2026 consolidation. Therefore the European rules below have official Commission support, but direct inspection of the consolidated legal PDF is incomplete. Do not represent this as successful independent legal-text verification. The official locator is [EUR-Lex consolidated AI Act](https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A02024R1689-20260727).

A production release gate should require a legal-content reviewer to reconcile the authoritative text and amendments. A blocked fetch should create a source-verification task, not silently reuse an old version or mark requirements absent.

Every source record needs URL, issuing authority, publication/version date when available, retrieval timestamp, content digest, retrieval completeness, legal status, provision locator, and reviewer. Track effective date separately from the date an obligation applies. A proposed rule remains proposed even if its expected application date is known.

## 3. EU timeline: evaluate dates, do not hardcode folklore

The Commission timeline reflects the Digital Omnibus amendments. It reports Article 50 transparency applying from 2 August 2026, Annex III high-risk rules from 2 December 2027, and Annex I regulated-product high-risk rules from 2 August 2028. It identifies a 2 December 2026 transition for certain already-marketed synthetic-content systems under Article 50(2). General-purpose AI obligations began applying in August 2025, subject to applicable transitions. These are not the original high-risk deadlines often repeated in older material. [Commission implementation timeline](https://ai-act-service-desk.ec.europa.eu/en/ai-act/eu-ai-act-implementation-timeline)

Implement dates per rule and transition predicate. When a future obligation is relevant, output “readiness assessment: not yet applicable” rather than “violation.” Historical assessments must retain the legal version used at the time. New law should trigger reassessment, not rewrite an old report.

## 4. EU applicability decision tree

Use a questionnaire plus document evidence. Suggestions from code inspection can prefill questions, but must not decide legal classifications without review.

1. Identify the assessed object: ordinary software, an AI system, a general-purpose AI model, or an integrated product. Record unresolved definitions.
2. Identify each actor: provider, deployer, importer, distributor, product manufacturer, or several roles. Track material modifications and branding separately.
3. Establish territorial nexus: EU market/service placement, an EU deployer, or relevant third-country activity whose outputs are used in the Union.
4. Examine exclusions and exceptions with a reasoned record. Do not equate open-source licensing with complete exemption.
5. Screen prohibited practices before assessing readiness for permitted deployment.
6. Evaluate high-risk classification using the relevant product route or listed use case, including any classification exceptions.
7. Independently evaluate transparency duties and the separate GPAI model route. These are not merely subtypes of the high-risk route.
8. Select obligations by role, classification, assessment date, market date, transition conditions, and sectoral overlap.
9. Require reviewer disposition for uncertain predicates. Unknown must not default to out of scope.

Article 2 covers differentiated roles and certain third-country scenarios. Its open-source system exclusion has exceptions for relevant high-risk systems and systems covered by Articles 5 or 50. [Article 2](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-2)

The GPAI branch is distinct: qualifying open-source models can receive exemptions from specified documentation duties, but copyright-policy and public training-summary duties remain; systemic-risk models do not receive that documentation exemption. Inspect licensing, released parameters, architecture and usage information rather than relying on an “open” marketing label. [Article 53](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-53)

## 5. EU control families and evidence boundaries

### Lifecycle risk management

Maintain hazards, affected groups, foreseeable misuse, mitigations, predefined tests, residual risks, and change history. Automation can detect missing links and run approved test plans. Humans determine whether the hazard analysis is adequate and residual risk acceptable. Article 9 describes iterative lifecycle risk management and testing against predefined metrics appropriate to intended purpose. [Article 9](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-9)

### Technical resilience

Collect declared accuracy metrics, evaluation results, uncertainty, fault-injection tests, fallback behavior, adversarial tests and security findings. Where relevant, include poisoning, evasion, confidentiality and feedback-loop scenarios. Article 15 links accuracy, robustness and cybersecurity to context and lifecycle performance. A passing suite is bounded evidence, not proof against all attacks. [Article 15](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-15)

### Transparency

Test whether relevant interaction notices appear, whether applicable outputs contain machine-readable markings, and whether disclosures survive supported delivery channels. Record accessibility and first-exposure behavior. Provider and deployer duties differ; public-interest text, deepfakes, ordinary editing, and other exceptions require contextual review. Do not assert that every AI-generated sentence requires the same label. [Article 50](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-50)

### Documentation and organizational controls

Provide modules for data governance, technical documentation, logs, instructions, human oversight, quality management, deployer responsibilities and fundamental-rights assessments where applicable. These need individually reviewed legal mappings before production activation. Presence checks can establish that an artifact exists; sampled observation and reviewer evidence are needed to assess actual practice.

### Conformity route

Export evidence for the applicable procedure, not a self-issued regulatory certificate. Article 43 distinguishes internal-control routes, conditional notified-body involvement for certain biometric systems, and sectoral-product procedures. Not every high-risk system requires external certification. The lab must not claim notified-body status, CE conformity, or regulator approval merely because it generates a report. [Article 43](https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-43)

## 6. US architecture: baseline plus overlays

Build a voluntary NIST baseline separately from binding-law overlays. NIST describes AI RMF as voluntary; its current page says RMF 1.0 is being revised. Pin published RMF 1.0 and do not silently substitute an anticipated revision. Use Govern, Map, Measure and Manage as coverage categories, not a certification scheme. [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)

The July 2024 GenAI Profile adds useful risk-oriented actions. Translate them into evidence requirements and bounded tests for issues such as confabulation, privacy, harmful bias, information integrity, security, intellectual property and human-AI interaction. Do not imply that a universal hallucination or fairness threshold is prescribed. [NIST GenAI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)

US applicability proceeds through independent overlays:

1. Is the assessment voluntary, contractual, regulatory, or supporting federal procurement?
2. Which states/localities, affected individuals, customers and deployment locations are involved?
3. Does the use involve employment, lending, insurance, health, housing, education, minors, government services, or another regulated domain?
4. Which entity performs each regulated role?
5. Which enacted rules, exemptions, transitions, contracts or agency requirements apply?
6. Are relevant implementing rules final, proposed, contested, superseded or not directly verified?

Existing consumer-protection and civil-rights requirements can apply to AI. Do not model the US as either “unregulated” or governed universally by NIST. Federal agency policies also have bounded scopes rather than automatically governing every private software project. [Federal enforcement statement](https://search.ftc.gov/news-events/news/press-releases/2023/04/ftc-chair-khan-officials-doj-cfpb-eeoc-release-joint-statement-ai), [OMB policy announcement](https://www.whitehouse.gov/releases/2025/04/white-house-releases-new-policies-on-federal-agency-ai-use-and-procurement/)

## 7. Starter US overlays

### NYC employment tools

The official city page identifies covered automated employment decision tool obligations including an audit within the preceding year, publicly available audit information and required notices. The lab can calculate metrics, check timestamps and inspect publication artifacts. Applicability, auditor independence, data adequacy and procedural sufficiency require review. A metric computation is not itself an independent audit. [NYC DCWP](https://www.nyc.gov/site/dca/about/automated-employment-decision-tools.page)

### Colorado consequential decisions

Do not ship stale SB24-205 assumptions. Colorado's official enacted-bill summary states that SB26-189 repeals and reenacts those provisions, with covered ADMT requirements beginning 1 January 2027. These include developer documentation, material-update notices, records, consumer disclosures, correction and human-review mechanisms. Implementing rulemaking remained in progress on this chapter's baseline date. [Enacted SB26-189](https://leg.colorado.gov/bills/sb26-189), [Attorney General rulemaking](https://coag.gov/ai/)

Use a future-readiness pack for enacted requirements and a separate disabled draft-rule preview. Do not import draft implementation details into binding checks. Require current legal review before enabling enforcement-oriented classifications.

### Medical software

Medical-device applicability is not inferable from the presence of an LLM. Intended use and sectoral classification matter. FDA's register distinguishes final guidance from its draft AI lifecycle document. A draft recommendation must not be labelled a statutory obligation; a lab's test report cannot substitute for applicable authorization. [FDA digital-health guidance register](https://www.fda.gov/medical-devices/digital-health-center-excellence/guidances-digital-health-content)

## 8. Implementable rule and result records

The following is a generic schema illustration, not an authoritative encoding of a complete legal provision:

```yaml
rule_id: eu.article50.interaction_notice.evidence
pack_version: 0.1.0-review
baseline_date: 2026-10-21
source:
  url: https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-50
  authority: European Commission
  provision: Article 50(1)
  legal_kind: binding_legislation
  direct_consolidated_text_verified: false
applicability:
  expression: reviewer_confirmed_article50_1_scope
  unknown_result: review_required
  temporal_resolver: eu_article50_transition_rules
checks:
  - id: first_interaction_notice_observed
    method: browser_synthetic_fixture
    expected: notice_visible_before_or_at_first_interaction
human_review:
  - territorial_and_role_scope
  - applicable_exceptions
  - contextual_sufficiency_and_accessibility
output:
  technical_status: not_run
  evidence_status: missing
  legal_assessment: unreviewed
```

A result should also contain system/model hashes, dataset identity, test version, execution environment, observation timestamp, scope limitations, confidence intervals where meaningful, artifact references, reviewer identity, and immutable change history. Avoid including personal data when synthetic or aggregated evidence suffices.

Use explicit technical statuses: passed, failed, inconclusive, not run. Keep evidence statuses separate: sufficient for specified check, incomplete, stale, unavailable. Legal assessment is separately unreviewed, applicability confirmed, not applicable with rationale, or reviewer conclusion. Only an authorized reviewer may change that final category.

## 9. Synthetic acceptance tests

1. Unknown deployment territory: the applicability resolver returns review required, not exempt.
2. Open-source high-risk fixture: the license flag does not disable every EU requirement.
3. Missing dataset provenance: risk benchmark passes but evidence completeness remains incomplete.
4. Future Colorado assessment: an October 2026 report distinguishes readiness gaps from currently applicable violations.
5. Draft-rule ingestion: the new source appears in preview and cannot alter production binding rules without approval.
6. Notice rendered after the first interaction: the technical check fails despite a policy file saying notices exist.
7. Output marker stripped in an export path: that path fails while unaffected formats retain their own results.
8. Audit expired by one day: freshness check flags it; legal review confirms applicability and date semantics.
9. Model changed after evaluation: prior evidence becomes potentially stale and reassessment is queued.
10. Robustness suite passes but reviewer rejects residual risk: technical status remains passed; legal/governance disposition does not become approved.
11. Official source unavailable: retain prior signed pack, flag review overdue, and never claim fresh verification.
12. Historical report replay: the exact old pack and evidence snapshot reproduce the historical result, while a new pack produces a separately identified assessment.

## 10. Release gate

Ship reviewed packs with signed manifests, source snapshots, semantic diffs, migration notes and reproducible fixtures. Changes to applicability, legal status, dates or interpretation require review even when no test code changes. Set a review cadence and event-driven checks for official amendments; do not silently activate changes.

The deliverable is an auditable chain from source to interpretation to applicability to evidence to bounded finding. That chain makes the lab useful across jurisdictions without pretending software can replace legal judgment or formal conformity procedures.
