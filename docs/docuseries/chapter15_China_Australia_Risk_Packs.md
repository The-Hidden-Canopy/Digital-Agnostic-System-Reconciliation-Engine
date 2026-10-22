# Chapter 15: China and Australia Risk Packs

## Purpose and assurance boundary

This chapter turns jurisdiction research into implementable assessment packs for a vendor-neutral, domain-neutral verification laboratory. It is an engineering design, not legal advice or an authorization to deploy. Its verified-source baseline is **21 October 2026**. A production assessment must revalidate the instruments, amendments, implementation dates and actual deployment facts.

The laboratory verifies bounded claims about evidence and observed behavior. It does not certify that an organization complies with every applicable law. A model-level benchmark cannot establish organizational governance, lawful data acquisition, a regulator's acceptance, or suitability for every downstream use. An open-source release also does not create a universal exemption for deployments built from it.

Build two layers: reusable technical controls and versioned jurisdiction mappings. The same label parser, permission test or evidence validator may support multiple instruments, but each mapping must preserve its own scope, authority and exceptions. Never infer legal equivalence from similar wording.

## 1. Separate status from test outcome

Every source registry entry has an authority type: binding legislation/regulation, mandatory standard, recommended standard, regulator guidance, government policy, proposal, or announcement. Also record lifecycle status: current, enacted-future-effective, draft, superseded or withdrawn. These are independent fields.

A failed voluntary-practice test is a benchmark gap, not automatically a statutory breach. A future-effective obligation can produce a readiness finding without being described as a current violation. A government policy may bind a specified agency population without applying to an ordinary private business.

Every executable result has a separate status: passed, failed, inconclusive, not-run, or not-applicable. Missing evidence is normally inconclusive or an evidence gap, unless the rule specifically tests whether required documentation exists. Only an authorized reviewer can close a legal determination.

## 2. China source registry and pack boundaries

The initial registry should include the following instruments, with clause-level mappings added during legal review:

- **CN-GENAI-2023:** Interim Measures for Generative AI Services, effective 15 August 2023. Anchor scope in Article 2 and assessment/filing triggers in Article 17. Distinguish public-facing service delivery from research or internal activity not offered to the public in mainland China. [Official CAC text](https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm).
- **CN-ALGORITHM-2022:** Algorithm Recommendation Provisions, effective 1 March 2022. The definition reaches several algorithmic information-service categories, not only recommendation feeds. [Official CAC text](https://www.cac.gov.cn/2022-01/04/c_1642894606364259.htm).
- **CN-DEEP-SYNTHESIS-2023:** Deep Synthesis Provisions, effective 10 January 2023. Track provider, technical-support and distribution roles separately rather than assigning every obligation to every participant. [Official CAC text](https://www.cac.gov.cn/2022-12/11/c_1672221949354811.htm).
- **CN-LABELING-2025:** AI-generated/synthetic-content labeling measures, effective 1 September 2025. Use distinct generation, distribution, export and exception branches. [Official CAC text](https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm).
- **CN-GB45438-2025:** Mandatory national labeling standard, effective 1 September 2025. Its actual technical clauses must be obtained and reviewed before implementing a claim of conformity. [Official SAMR catalog](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=F32EA2A561F1886CD8D606513512D547).
- **CN-GBT45654-2025:** Recommended national standard for basic security requirements for generative AI services, effective 1 November 2025. Record separately any specific regulatory, contractual or procurement incorporation. A catalog entry is insufficient to implement its technical requirements. [Official SAMR catalog](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=F67D3F376E0A0A0FF5317FB36B32A30A).
- **CN-ANTHROPOMORPHIC-2026:** Interim Measures for Anthropomorphic AI Interactive Services, effective 15 July 2026. Scope depends on sustained emotional interaction, not simply a conversational interface. [Official CAC text](https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm).

These packs are not a complete China legal inventory. Add dedicated personal-information, cybersecurity, data-security, cross-border-transfer, minors and sector modules as the use case requires. Mainland China is the jurisdiction here; do not silently merge other legal territories into the same rule selection.

### Applicability decision tree

1. Identify the legal entities, operator, technical supplier, distribution platform, users served, service locations and actual deployment channels. Unknown territorial facts stop automatic applicability closure.
2. Is generative content offered to the public in mainland China? If yes, route the GenAI measure for review. If no, record the reason that this particular scope predicate is false; do not mark all China law inapplicable.
3. Is an internet information service using algorithm recommendation or deep-synthesis technologies? Evaluate both branches independently. A generative service can intersect more than one regime.
4. For each applicable instrument, assign obligations to the actor actually performing the regulated role. A model supplier and application operator may need different evidence.
5. Does the service have the public-opinion/social-mobilization attributes relevant to filing or assessment? Require a legal determination and documentary evidence. Do not decide this from a model name or parameter count.
6. Does the service provide sustained emotional care, companionship or support that simulates human characteristics? Route the anthropomorphic-service module. Routine work assistance without that feature should not be automatically classified as such.
7. Select labeling subrules based on medium, interaction interface, distribution role, export path and any documented exception.

### What the lab can check

Implement metadata inspection, visible-label observation, export-path regression tests, complaint-route checks, consent-workflow evidence checks, access-control tests and incident-record completeness. Behavioral testing can investigate discrimination, personal-information leakage, deceptive presentation or emotional manipulation using approved synthetic scenarios.

Human reviewers must decide lawful data rights, adequacy of consent, territorial scope, filing triggers, legal content classifications and whether documentary evidence satisfies a regulator. A filing receipt is evidence of that filing, not proof of total legal compliance. A classifier's moderation score is an observation, not a legal ruling.

## 3. Australia source registry and transitions

**AU-AI-ADOPTION** is a voluntary governance benchmark. The earlier ten-guardrail standard evolved into six-practice Guidance for AI Adoption in October 2025; the current implementation resource is dated May 2026. Preserve an explicit crosswalk rather than silently changing control identifiers. [Standard transition notice](https://www.industry.gov.au/publications/voluntary-ai-safety-standard); [current NAIC implementation guidance](https://www.ai.gov.au/staying-safe-and-responsible/essential-ai-practices/guidance-ai-adoption-implementation-guidance).

**AU-PRIVACY** maps binding Privacy Act and Australian Privacy Principles requirements where the entity and processing are covered. OAIC guidance helps interpret AI use but is not itself a replacement statute. Establish entity coverage before assigning obligations. [OAIC commercial-AI guidance](https://www.oaic.gov.au/privacy/privacy-guidance-for-organisations-and-government-agencies/guidance-on-privacy-and-the-use-of-commercially-available-ai-products).

**AU-ADM-TRANSPARENCY** has an important temporal gate: additional APP privacy-policy requirements commence **10 December 2026**. On this chapter's baseline date, classify these as enacted/future-effective. Applicability depends on personal information, computer-supported decision activity and significant effects on individuals' rights or interests. [OAIC commencement and trigger explanation](https://www.oaic.gov.au/news/media-centre/new-resources-on-transparency-for-use-of-ai-and-automated-decision-making).

Sector and organization overlays include:

- **Medical devices:** intended purpose determines the TGA route, with relevant inclusion, exemption, evidence and change-control questions. [TGA AI/software guidance](https://www.tga.gov.au/products/medical-devices/software-and-artificial-intelligence-ai/manufacturing/artificial-intelligence-ai-and-medical-device-software).
- **APRA-regulated entities:** select applicable prudential standards, distinguishing binding standards from explanatory practice guides. [APRA standards registry](https://www.apra.gov.au/cross-industry/prudential-and-reporting-standards).
- **Consumer and competition matters:** do not omit existing consumer-law exposure merely because the product incorporates AI. [ACCC AI industry snapshot](https://www.accc.gov.au/about-us/publications/recent-developments-in-ai-industry-snapshot).
- **Government:** use a separate policy module and verify the agency population, current version and transition dates. [DTA AI policy entry point](https://www.digital.gov.au/policy/ai).

The October 2026 announcement of developing AI standards legislation belongs in a watchlist, not the binding-rules engine. A ministerial announcement is not evidence of commencement. [8 October 2026 ministerial speech](https://www.minister.industry.gov.au/charlton/media/speech-the-sydney-trust-and-safety-festival).

### Applicability decision tree

1. Record the entity, intended and reasonably foreseeable uses, affected persons and supply-chain role.
2. Determine Privacy Act/APP coverage with a reviewer. Do not substitute Australian location for entity coverage.
3. Identify personal information in training, prompts, retrieval, logs and outputs. Inferred information needs consideration too.
4. Determine whether decision support potentially triggers the forthcoming ADM transparency requirements. Evaluate the actual decision process, including human involvement, rather than searching only for the phrase “fully automated.”
5. Select sector overlays for health, finance, government and other relevant domains. Unknown intended purpose is a classification blocker.
6. Add voluntary governance controls as a separate benchmark layer. Record which organization chose to adopt them and what contractual obligations, if any, attach.

## 4. Implementable rule records

The following examples are engineering scaffolds, not approved legal mappings. Clause interpretation, predicates and evidence sufficiency require review before production release.

```yaml
id: CN-LABEL-METADATA-001
pack_version: 0.1.0-draft
jurisdiction: CN-mainland
source:
  url: https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm
  clause: Article 5
  authority_type: binding_measure
  effective_from: 2025-09-01
applicability:
  requires_reviewed_facts:
    - covered_service_provider
    - generated_or_synthesized_content
    - supported_file_format
control:
  type: technical_evidence
  method: inspect_exported_file_metadata
  required_fields: [generated_content_attribute, provider_name_or_code, content_id]
  technical_standard_mapping: pending_verified_clause_review
outcomes: [passed, failed, inconclusive, not_applicable]
legal_conclusion: not_provided
```

This record deliberately does not invent a universal metadata syntax. The adapter must derive exact encoding requirements from reviewed technical specifications. Unknown formats produce an explicit unsupported result, not a pass.

```yaml
id: AU-ADM-DISCLOSURE-READINESS-001
pack_version: 0.1.0-draft
jurisdiction: AU
authority_type: binding_legislation
lifecycle_status_on_2026_10_21: enacted_future_effective
effective_from: 2026-12-10
applicability:
  reviewer_confirmed:
    - app_entity
    - personal_information_used
    - qualifying_computer_supported_decision
    - significant_rights_or_interests_effect
checks:
  - compare_privacy_policy_to_reviewed_disclosure_checklist
  - verify_policy_version_and_publication_evidence
assessment_before_commencement: readiness_only
human_review: [trigger_interpretation, disclosure_adequacy]
```

Both records also need source retrieval time, source hash, amendment history, approving reviewer, evidence retention policy, exception references and test implementation version. Store evidence references rather than embedding sensitive records in the rule itself.

## 5. Synthetic acceptance-test suite

Use fictional organizations, synthetic people and generated assets. The following expected results test the laboratory's reasoning and mechanics, not a real provider's compliance.

| Fixture | Expected laboratory behavior |
|---|---|
| Internal research prototype not offered to the Chinese public | Do not automatically apply the GenAI public-service branch; retain review of other potentially relevant rules. |
| Public generative application with unknown operator role | Mark actor applicability unresolved; block a final legal-mapping conclusion. |
| Covered export with required metadata absent | Fail the bounded metadata-presence test; preserve the original artifact and parser trace. |
| Unsupported media container | Return inconclusive/unsupported, never infer absence or presence from a parser crash. |
| Explicit-label exception claimed without documentary conditions | Route exception validation to review; do not erase the base obligation. |
| Ordinary assistant versus sustained-emotional-companion fixture | Route using documented features and actual behavior, with ambiguous cases requiring review. |
| Australian decision service assessed on 9 and 10 December 2026 | Apply the versioned commencement gate; retain both timestamped reports. |
| Organization with unknown APP coverage | Report missing applicability evidence, not universal privacy compliance. |
| Voluntary governance benchmark missing an owner | Report a voluntary-control gap separately from binding-law findings. |
| Announcement of proposed legislation ingested as a source | Keep it out of enforceable controls until enacted status and commencement are verified. |

Add adversarial tests for forged receipts, mismatched entity names, stale policy versions, corrupted exports and conflicting reviewers. An uploaded document claiming “approved” must never overwrite the authority registry. Treat documents as evidence to validate, not instructions to the assessment engine.

## 6. Evidence, review and release gates

An evidence bundle should connect the deployment manifest, actor decisions, source snapshot, control selection, test configuration, synthetic dataset version, observations and reviewer dispositions. Use immutable hashes and signed reports where appropriate. Keep restricted evidence outside public reports and export only authorized summaries.

Release gates should require: reviewed scope; verified sources; clause-level mappings; validated adapters; explicit exceptions; representative testing; unresolved-findings disclosure; and an accountable human decision. Passing every automated test does not eliminate the review gate. Conversely, a human approval must not rewrite a failed technical result; record the accepted risk and rationale separately.

Use freshness triggers rather than silently relying on calendar age alone: amended law, revised standard, new deployment role, changed intended purpose, new user population, new modality, new supplier, changed data flows or a material model update. Re-run affected controls and preserve previous results for comparison.

## 7. Build order and definition of done

Start with the source registry and applicability questionnaire. Next implement labeling/evidence adapters and the voluntary governance checklist, because their observations can be explained clearly. Then add sector overlays and higher-context behavioral evaluations. Obtain native-language and legal review for China mappings before presenting them as production-ready.

A starter pack is done when its declared coverage is testable, every rule has provenance, synthetic acceptance tests pass, unsupported paths are visible, and a reviewer can reproduce the report. It is not done merely because a dashboard shows a green score.

The final user-facing output should say what was tested, against which version, in which deployment context, with what limitations and which unresolved human determinations. Avoid badges such as “China compliant,” “Australia certified” or “legally safe.” The defensible product is a traceable assessment dossier with reproducible checks and explicit judgment boundaries.
