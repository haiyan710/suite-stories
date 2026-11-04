"""Evidence-focused specialist with exclusive access to web/page tools."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from typing import Any, Callable

from ..config import Settings
from ..llm.base import LLMError, LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from ..orchestration.handoffs import ResearchResult, ResearchTask
from ..orchestration.planning import EvidenceConfidence
from ..prompts import load_prompt
from ..storage import Database
from ..tools.assets import find_candidate_assets
from ..tools.documents import Document, page_text
from ..tools.pages import FetchedPage, PageFetcher, condense_page_text, host_of, normalize_url
from ..tools.web import (
    BRAND, DESTINATION, EDITORIAL_PRESS, PUBLICATION, OFFICIAL,
    OTHER, REVIEW_SIGNAL, SearchHit, WebSearchTool, _looks_like_hotel_site, classify_source, confidence_for,
)

logger = logging.getLogger(__name__)
# Each chunk yields at most 18 facts, so smaller chunks keep detail from long documents.
_DOC_CHUNK_CHARS = 8_000
_DOC_MAX_CHUNKS = 10
_QUERY_STOPWORDS = frozenset(
    "a an the and or of to in on at for from by with is are was were be it its this that "
    "these those what which who whether does do did any all not only before after as "
    "verify confirm check fetch find establish official page pages verified evidence "
    "claim claims snippet source sources".split()
)


class ResearchAgent:
    name = "researcher"

    def __init__(
        self, settings: Settings, provider: LLMProvider, db: Database, *,
        fetcher: PageFetcher | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._db = db
        self._search = WebSearchTool(provider, publication_domain=settings.publication_domain)
        self._fetcher = fetcher or PageFetcher(timeout_seconds=settings.fetch_timeout_seconds)
        self._progress: Callable[[str], None] = lambda _stage: None

    def set_progress_listener(self, callback: Callable[[str], None]) -> None:
        self._progress = callback

    @property
    def search_error(self) -> str:
        """Last live-search failure since reset_search_status (quota, outage)."""
        return self._search.last_search_error

    def reset_search_status(self) -> None:
        self._search.last_search_error = ""

    def run(self, task: ResearchTask) -> ResearchResult:
        if task.followup_questions and task.existing_research:
            return self._run_followup(task)
        self._progress("official")
        hits = self._initial_hits(task)
        pages = self._fetch_hits(hits)
        if not self._has_valid_fetched_property_page(hits, pages, task.hotel_name):
            supplemental = self._search.discover_public_official(task.hotel_name)
            existing = {normalize_url(hit.url).rstrip("/") for hit in hits}
            supplemental = [
                hit for hit in supplemental
                if normalize_url(hit.url).rstrip("/") not in existing
            ][:4]
            if supplemental:
                hits.extend(supplemental)
                pages.update(self._fetch_hits(supplemental))
        evidence, assets, source_records = self._extract_and_persist(
            task.project_id, hits, pages, hotel_name=task.hotel_name
        )
        if not source_records:
            return ResearchResult(
                status="needs_clarification",
                property_identity={"hotel": task.hotel_name},
                uncertainties=["No credible search result was found for this property."],
                recommended_followup_questions=["Confirm the full hotel name or provide its official URL."],
            )
        return self._synthesize(task, evidence, assets, source_records)

    @classmethod
    def _has_valid_fetched_property_page(
        cls, hits: list[SearchHit], pages: dict[str, FetchedPage], hotel_name: str,
    ) -> bool:
        for hit in hits:
            if hit.source_type not in {OFFICIAL, BRAND}:
                continue
            page = pages.get(hit.url)
            if page and page.fetched_ok and cls._page_matches_hotel(page, hotel_name):
                return True
        return False

    def _initial_hits(self, task: ResearchTask) -> list[SearchHit]:
        self._progress("coverage")
        searches = [
            lambda: self._search.search_official_hotel(task.hotel_name)[:4],
            lambda: self._search.search_publication(task.hotel_name)[:2],
            lambda: self._search.search_destination_context(task.hotel_name, task.city_hint)[:3],
            lambda: self._search.search_editorial_coverage(task.hotel_name)[:3],
        ]
        if task.trip_nights:
            searches.append(lambda: self._search.search_itinerary_context(task.hotel_name, task.city_hint)[:4])
        # Independent searches; results are concatenated in the order above.
        out: list[SearchHit] = [hit for hits in self._parallel_map(lambda run: run(), searches) for hit in hits]
        if task.article_url:
            out.insert(0, SearchHit(
                task.article_url, "User-supplied article", "", PUBLICATION, "high", grounded=True
            ))
        seen: set[str] = set()
        deduped: list[SearchHit] = []
        for hit in out:
            normalized = normalize_url(hit.url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(hit)
        return deduped[:12 if task.trip_nights else 8]

    def _parallel_map(self, fn: Callable[[Any], Any], items: list[Any]) -> list[Any]:
        """Map fn over items with bounded concurrency, preserving input order.

        Used for independent LLM calls (searches, per-page extraction); the
        provider calls are stateless, so they are safe to run side by side.
        """
        if len(items) <= 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=self._settings.max_concurrent_llm_calls) as pool:
            return list(pool.map(fn, items))

    def _fetch_hits(self, hits: list[SearchHit]) -> dict[str, FetchedPage]:
        pages: dict[str, FetchedPage] = {}
        with ThreadPoolExecutor(max_workers=self._settings.max_concurrent_fetches) as pool:
            futures = {pool.submit(self._fetcher.fetch, hit.url): hit for hit in hits}
            for future in as_completed(futures):
                hit = futures[future]
                try:
                    pages[hit.url] = future.result()
                except Exception as exc:
                    logger.warning("research fetch failed url=%s error=%s", hit.url, exc)
        return pages

    def _extract_and_persist(
        self, project_id: int, hits: list[SearchHit], pages: dict[str, FetchedPage], *,
        sid_start: int = 1, hotel_name: str = "", trusted_hosts: set[str] | None = None,
        experience: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        evidence: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        source_records: list[dict[str, Any]] = []
        validated_hosts = set(trusted_hosts or set())
        rows: list[tuple[int, SearchHit, FetchedPage | None, bool, str, bool]] = []
        for index, hit in enumerate(hits, sid_start):
            page = pages.get(hit.url)
            fetched_ok = bool(page and page.fetched_ok)
            if not fetched_ok and not hit.grounded:
                # A URL the model wrote without a live search result behind it, and
                # that we could not open, is unverified in every respect: drop it.
                logger.info("dropping ungrounded unfetchable candidate url=%s", hit.url)
                continue
            source_type = hit.source_type
            if not source_type or source_type == OTHER:
                source_type = classify_source(
                    hit.url, publication_domain=self._settings.publication_domain
                )
            if (
                source_type == OTHER and hotel_name
                and _looks_like_hotel_site(hit.url, hotel_name)
            ):
                source_type = OFFICIAL
            candidate_host = host_of(hit.url)
            identity_mismatch = bool(
                fetched_ok and page and hotel_name
                and source_type in {OFFICIAL, BRAND}
                and candidate_host not in validated_hosts
                and not self._page_matches_hotel(page, hotel_name)
            )
            if identity_mismatch:
                logger.info(
                    "rejecting hotel source identity mismatch hotel=%s url=%s title=%s",
                    hotel_name, hit.url, page.title,
                )
                source_type = OTHER
            elif fetched_ok and source_type in {OFFICIAL, BRAND} and candidate_host:
                validated_hosts.add(candidate_host)
            rows.append((index, hit, page, fetched_ok, source_type, identity_mismatch))
        # Page extraction is the slow part (one LLM call per page) and pages are
        # independent, so run it concurrently; persistence below stays in S# order.
        extracted = self._parallel_map(
            lambda row: (
                (
                    self._extract_guest_experience(f"S{row[0]}", row[2], hotel_name) if experience
                    else self._extract_page_evidence(f"S{row[0]}", row[4], row[2])
                )
                if row[3] and row[2] and not row[5] else []
            ),
            rows,
        )
        for (index, hit, page, fetched_ok, source_type, identity_mismatch), page_facts in zip(rows, extracted):
            sid = f"S{index}"
            title = (page.title if fetched_ok and page else hit.title) or hit.url
            confidence = confidence_for(source_type, fetched_ok=fetched_ok)
            db_id = self._db.upsert_source(
                project_id=project_id, url=hit.url,
                url_normalized=normalize_url(hit.url), title=title,
                source_type=source_type, confidence=confidence, fetched_ok=fetched_ok,
            )
            facts: list[dict[str, Any]] = []
            if fetched_ok and page and not identity_mismatch:
                facts = page_facts
                confidence = self._evidence_confidence(source_type, fetched_ok=True)
                for fact in facts:
                    fact["confidence"] = confidence
                    if experience:
                        fact["evidence_level"] = "guest_report"
                if source_type in {OFFICIAL, BRAND}:
                    assets.extend(asdict(a) for a in find_candidate_assets(page, page_source_type=source_type))
            # Some authoritative hotel sites reject automated page fetches. A real
            # search result is still useful as lower-confidence evidence, and is
            # materially different from an unidentified property. Keep its exact
            # snippet with explicit provenance instead of asking the user to
            # reconfirm a hotel name that was already resolved by search.
            if (
                not facts
                and hit.grounded
                and hit.snippet.strip()
                and source_type in {OFFICIAL, BRAND, PUBLICATION, DESTINATION, EDITORIAL_PRESS}
            ):
                facts = [{
                    "fact": hit.snippet.strip(),
                    "dimension": "search_result_snippet",
                    "quote": "",
                    "source_ids": [sid],
                    "evidence_level": "search_snippet",
                    "confidence": EvidenceConfidence.SEARCH_SNIPPET_ONLY.value,
                }]
            if db_id is not None:
                self._db.add_evidence_to_source(
                    db_id, "\n".join(f"- {f.get('fact', '')}" for f in facts)
                )
            evidence.extend(facts)
            source_records.append({
                "sid": sid, "url": hit.url, "title": title,
                "source_type": source_type, "confidence": confidence,
                "fetched_ok": fetched_ok, "facts": facts,
            })
        return evidence, assets[:80], source_records

    @staticmethod
    def _page_matches_hotel(page: FetchedPage, hotel_name: str) -> bool:
        """Validate a candidate's page identity before treating it as official."""
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        expected = normalize(hotel_name)
        haystack = normalize(" ".join((page.title, page.meta_description, page.text[:4000])))
        if expected and expected in haystack:
            return True
        tokens = [token for token in expected.split() if len(token) > 2]
        if not tokens:
            return False
        required = 1 if len(tokens) == 1 else 2
        return sum(bool(re.search(rf"\b{re.escape(token)}\b", haystack)) for token in tokens) >= required

    def _extract_page_evidence(
        self, sid: str, source_type: str, page: FetchedPage,
    ) -> list[dict[str, Any]]:
        prompt = (
            "Extract verified hotel/destination evidence. Return a JSON array, maximum 18. "
            "Each item: {\"fact\":\"\",\"dimension\":\"\",\"quote\":\"\"}.\n"
            f"SOURCE_ID={sid} TYPE={source_type} URL={page.url}\n\n"
            + condense_page_text(page)
        )
        try:
            response = self._provider.complete(
                [LLMMessage(role="user", content=prompt)], system=load_prompt("researcher"),
                max_tokens=2600, temperature=0.15,
            )
        except LLMError as exc:
            # One unreadable page (e.g. a model safety filter tripping on its text)
            # must not fail the whole research run; the page just yields no facts.
            logger.warning("page extraction failed sid=%s url=%s: %s", sid, page.url, str(exc)[:160])
            return []
        data = extract_json_block(response.text)
        items = data if isinstance(data, list) else []
        out: list[dict[str, Any]] = []
        for item in items[:18]:
            if isinstance(item, dict) and item.get("fact"):
                item["source_ids"] = [sid]
                out.append(item)
        return out

    def _extract_guest_experience(self, sid: str, page: FetchedPage, hotel_name: str) -> list[dict[str, Any]]:
        """What guests and reviewers report about staying at the hotel (not brochure facts)."""
        prompt = (
            f"HOTEL={hotel_name}\nSOURCE_ID={sid} URL={page.url}\n\n" + condense_page_text(page)
        )
        try:
            response = self._provider.complete(
                [LLMMessage(role="user", content=prompt)], system=load_prompt("guest_experience"),
                max_tokens=2600, temperature=0.15,
            )
        except LLMError as exc:
            logger.warning("guest extraction failed sid=%s url=%s: %s", sid, page.url, str(exc)[:160])
            return []
        data = extract_json_block(response.text)
        out: list[dict[str, Any]] = []
        for item in (data if isinstance(data, list) else [])[:18]:
            if isinstance(item, dict) and item.get("fact"):
                item["source_ids"] = [sid]
                out.append(item)
        return out

    def _synthesize(
        self, task: ResearchTask, evidence: list[dict[str, Any]],
        assets: list[dict[str, Any]], source_records: list[dict[str, Any]],
    ) -> ResearchResult:
        prompt = (
            "Synthesize the evidence package. Return JSON with keys: property_identity, "
            "verified_facts, experience_signals, destination_relationship, audience_relevance, "
            "existing_publication_content, uncertainties, contradictions, "
            "recommended_followup_questions, critical_unknowns, property_dna, classification. "
            "Every verified_fact and experience_signal must retain source_ids and confidence from "
            "the evidence. Do not upgrade search_snippet_only or inferred evidence. Do not create an "
            "editorial proposition.\n\nHOTEL=" + task.hotel_name
            + "\nAUDIENCE=" + task.audience
            + "\nTASK BRIEF=" + (task.brief.to_prompt() if task.brief else "")
            + "\nEVIDENCE=" + json.dumps(evidence, ensure_ascii=False)[:18000]
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt("researcher"),
            max_tokens=4200, temperature=0.2,
        )
        data = extract_json_block(response.text)
        if not isinstance(data, dict):
            data = {}
        has_fetched_page = any(item.get("fetched_ok") for item in source_records)
        verified_facts = self._normalize_synthesized_evidence(
            data.get("verified_facts") or evidence, evidence
        )
        experience_signals = self._normalize_synthesized_evidence(
            data.get("experience_signals") or [], evidence
        )
        return ResearchResult(
            status="complete" if evidence and has_fetched_page else "partial",
            property_identity=data.get("property_identity") or {"hotel": task.hotel_name},
            verified_facts=verified_facts,
            experience_signals=experience_signals,
            destination_relationship=data.get("destination_relationship") or [],
            audience_relevance=data.get("audience_relevance") or [],
            existing_publication_content=data.get("existing_publication_content") or [],
            available_assets=assets,
            source_records=source_records,
            uncertainties=data.get("uncertainties") or [],
            contradictions=data.get("contradictions") or [],
            recommended_followup_questions=data.get("recommended_followup_questions") or [],
            property_dna=data.get("property_dna") or {},
            classification=data.get("classification") or {},
            critical_unknowns=list(data.get("critical_unknowns") or []),
        )

    def _run_followup(self, task: ResearchTask) -> ResearchResult:
        existing = ResearchResult.from_dict(task.existing_research)
        extra_facts: list[dict[str, Any]] = []
        extra_sources: list[dict[str, Any]] = []
        evidence_by_claim: list[dict[str, Any]] = []
        self._progress("official")
        trusted_hosts = {
            host_of(str(source.get("url", "")))
            for source in existing.source_records
            if source.get("fetched_ok") and source.get("source_type") in {OFFICIAL, BRAND}
        }
        trusted_hosts.discard("")
        coverage = task.destination or task.experience
        questions = task.followup_questions[:6 if coverage else 4]
        gathered = self._parallel_map(
            lambda question: (lambda hits: (hits, self._fetch_hits(hits[:4])))(self._followup_hits(task, question)),
            questions,
        )
        for question, (hits, pages) in zip(questions, gathered):
            facts, _assets, sources = self._extract_and_persist(
                task.project_id, hits[:4], pages,
                sid_start=len(existing.source_records) + len(extra_sources) + 1,
                hotel_name=task.hotel_name,
                trusted_hosts=trusted_hosts, experience=task.experience,
            )
            extra_facts.extend(facts)
            extra_sources.extend(sources)
            evidence_by_claim.append({
                "claim": question,
                "facts": facts,
                "sources": [{
                    "url": source.get("url", ""),
                    "title": source.get("title", ""),
                    "source_type": source.get("source_type", ""),
                    "confidence": source.get("confidence", ""),
                    "fetched_ok": source.get("fetched_ok", False),
                    "facts": source.get("facts", []),
                } for source in sources],
            })
        existing.verified_facts.extend(extra_facts)
        existing.source_records.extend(extra_sources)
        existing.version += 1
        if coverage:
            # Venue and guest-experience rounds add coverage; they are not claims to
            # verify, so they must not replace the open questions (which would send the
            # next verification round after restaurants on the hotel's blocked site).
            return existing
        existing.status = "complete" if extra_facts else "partial"
        existing.verification_claims = self._assess_followup_claims(evidence_by_claim)
        existing.uncertainties = [
            item["claim"] for item in existing.verification_claims
            if item.get("status") == "uncertain"
        ]
        existing.contradictions = [
            item["claim"] for item in existing.verification_claims
            if item.get("status") == "unsupported"
        ]
        existing.critical_unknowns = list(existing.uncertainties)
        return existing

    def ingest_documents(
        self, project_id: int, research: ResearchResult, documents: list[Document],
    ) -> ResearchResult:
        """Add user-shared hotel documents (decks, meeting notes) as official evidence.

        These come from the property itself, so their facts are verified_official.
        Long documents are read in chunks so later pages are not silently dropped.
        """
        for document in documents:
            if not document.ok:
                continue
            sid = f"S{len(research.source_records) + 1}"
            title = f"{document.name} (shared in Slack)"
            text = document.text
            chunks = [
                FetchedPage(
                    url=document.url, status=200, title=title, text=text[start:start + _DOC_CHUNK_CHARS],
                    meta_description="", headings=(), images=(), links=(), fetched_ok=True,
                )
                for start in range(0, min(len(text), _DOC_CHUNK_CHARS * _DOC_MAX_CHUNKS), _DOC_CHUNK_CHARS)
            ]
            facts = [
                fact for chunk_facts in self._parallel_map(
                    lambda page: self._extract_page_evidence(sid, OFFICIAL, page), chunks,
                ) for fact in chunk_facts
            ]
            confidence = self._evidence_confidence(OFFICIAL, fetched_ok=True)
            for fact in facts:
                fact["confidence"] = confidence
                fact.setdefault("evidence_level", "hotel_document")
            db_id = self._db.upsert_source(
                project_id=project_id, url=document.url, url_normalized=normalize_url(document.url),
                title=title, source_type=OFFICIAL,
                confidence=confidence_for(OFFICIAL, fetched_ok=True), fetched_ok=True,
            )
            if db_id is not None:
                self._db.add_evidence_to_source(db_id, "\n".join(f"- {f.get('fact', '')}" for f in facts))
            research.verified_facts.extend(facts)
            research.source_records.append({
                "sid": sid, "url": document.url, "title": title, "source_type": OFFICIAL, "private": True,
                "confidence": confidence, "fetched_ok": True, "facts": facts,
            })
        # Photos from hotel-supplied documents are the only images cleared for layouts.
        document_assets = [
            {
                "url": image.path, "local_path": image.path, "bucket": "hotel_document",
                "evidence": image.caption, "page": image.page, "orientation": image.orientation,
                "width": image.width, "height": image.height,
                # The deck page's own words name what the photo shows (e.g. which restaurant).
                "page_context": page_text(document.text, image.page)[:300],
                "page_url": document.url, "document": document.name, "source_type": OFFICIAL,
                "private": True, "rights": "hotel_supplied",
            }
            for document in documents if document.ok for image in document.images
        ]
        known = {asset.get("local_path") for asset in research.available_assets if isinstance(asset, dict)}
        research.available_assets = [
            asset for asset in document_assets if asset["local_path"] not in known
        ] + list(research.available_assets)
        research.version += 1
        return research

    def _followup_hits(self, task: ResearchTask, question: str) -> list[SearchHit]:
        """Bounded, claim-specific source fallback rather than one generic retry."""
        if task.destination or task.experience:
            return self._destination_hits(task, question)
        base = self._verification_query(task.hotel_name, question)
        official_hosts = []
        for source in task.existing_research.get("source_records", []) or []:
            if source.get("source_type") not in {OFFICIAL, BRAND}:
                continue
            # Do not keep steering retries back to an obsolete/blocked host.
            # It can remain in provenance, but only a successfully fetched host
            # is strong enough to constrain the next source-discovery query.
            if not source.get("fetched_ok"):
                continue
            host = host_of(str(source.get("url", "")))
            if host and host not in official_hosts:
                official_hosts.append(host)
        if task.verification_round <= 1:
            queries = [
                (f"site:{official_hosts[0]} {base}" if official_hosts else base + " current official property website"),
                base + " official hotel brand page press release PDF",
            ]
        else:
            queries = [
                base + " alternate-language official page PDF press release",
                base + " reputable hospitality travel design editorial review",
            ]
        collected: list[SearchHit] = []
        seen: set[str] = set()
        for query in queries:
            for hit in self._search.search(
                query,
                purpose="verify one specific claim using a different bounded source path",
                max_results=4,
            ):
                normalized = normalize_url(hit.url)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                source_type = hit.source_type
                if host_of(hit.url) in official_hosts:
                    source_type = OFFICIAL
                elif not source_type or source_type == OTHER:
                    source_type = classify_source(
                        hit.url, publication_domain=self._settings.publication_domain
                    )
                collected.append(replace(hit, source_type=source_type))
        priority = {OFFICIAL: 0, BRAND: 1, DESTINATION: 2, EDITORIAL_PRESS: 3, PUBLICATION: 4, OTHER: 5}
        collected.sort(key=lambda hit: priority.get(hit.source_type, 6))
        return collected[:6]

    @staticmethod
    def _evidence_confidence(source_type: str, *, fetched_ok: bool) -> str:
        if not fetched_ok:
            return EvidenceConfidence.SEARCH_SNIPPET_ONLY.value
        if source_type in {OFFICIAL, BRAND}:
            return EvidenceConfidence.VERIFIED_OFFICIAL.value
        if source_type in {DESTINATION, EDITORIAL_PRESS, PUBLICATION}:
            return EvidenceConfidence.VERIFIED_AUTHORITATIVE_SECONDARY.value
        return EvidenceConfidence.CORROBORATED_SECONDARY.value

    @staticmethod
    def _normalize_synthesized_evidence(
        items: list[Any], original_evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        confidence_by_sid = {
            sid: str(item.get("confidence") or EvidenceConfidence.INFERRED.value)
            for item in original_evidence
            for sid in item.get("source_ids", [])
        }
        rank = {
            EvidenceConfidence.UNSUPPORTED.value: 0,
            EvidenceConfidence.INFERRED.value: 1,
            EvidenceConfidence.SEARCH_SNIPPET_ONLY.value: 2,
            EvidenceConfidence.CORROBORATED_SECONDARY.value: 3,
            EvidenceConfidence.VERIFIED_AUTHORITATIVE_SECONDARY.value: 4,
            EvidenceConfidence.VERIFIED_OFFICIAL.value: 5,
        }
        out: list[dict[str, Any]] = []
        for raw in items:
            item = dict(raw) if isinstance(raw, dict) else {"fact": str(raw)}
            source_ids = [str(sid) for sid in item.get("source_ids", [])]
            allowed = [confidence_by_sid[sid] for sid in source_ids if sid in confidence_by_sid]
            supplied = str(item.get("confidence") or EvidenceConfidence.INFERRED.value).lower()
            if supplied not in rank:
                supplied = EvidenceConfidence.INFERRED.value
            if allowed:
                ceiling = max(allowed, key=lambda value: rank.get(value, 1))
                if rank.get(supplied, 1) > rank.get(ceiling, 1):
                    supplied = ceiling
            elif rank.get(supplied, 1) >= rank[EvidenceConfidence.CORROBORATED_SECONDARY.value]:
                supplied = EvidenceConfidence.INFERRED.value
            item["confidence"] = supplied
            out.append(item)
        return out

    def _destination_hits(self, task: ResearchTask, question: str) -> list[SearchHit]:
        """Venues and sights for an itinerary, from destination guides and travel press.

        Hosts that already failed to fetch for this project (blocked chain sites)
        are skipped, and fetchable guide/press pages are tried first.
        """
        records = task.existing_research.get("source_records", []) or []
        fetched = {host_of(str(r.get("url", ""))) for r in records if r.get("fetched_ok")}
        blocked = {host_of(str(r.get("url", ""))) for r in records if not r.get("fetched_ok")} - fetched
        collected: list[SearchHit] = []
        seen: set[str] = set()
        if task.experience:
            queries = (question, question + " review blog stayed")
            purpose = "find first-hand accounts of staying at this hotel: reviews, blogs, travel press"
        else:
            queries = (question, question + " guide Michelin Time Out tourism authority")
            purpose = "find specific named places for a day-by-day travel itinerary"
        for query in queries:
            for hit in self._search.search(query, purpose=purpose, max_results=5):
                normalized = normalize_url(hit.url)
                if not normalized or normalized in seen or host_of(hit.url) in blocked:
                    continue
                seen.add(normalized)
                source_type = hit.source_type
                if not source_type or source_type in {OTHER, OFFICIAL, BRAND}:
                    source_type = classify_source(
                        hit.url, publication_domain=self._settings.publication_domain
                    )
                collected.append(replace(hit, source_type=source_type))
        priority = (
            {EDITORIAL_PRESS: 0, OTHER: 1, REVIEW_SIGNAL: 1, DESTINATION: 2, PUBLICATION: 3, OFFICIAL: 4, BRAND: 5}
            if task.experience else
            {DESTINATION: 0, EDITORIAL_PRESS: 1, OTHER: 2, PUBLICATION: 3, OFFICIAL: 4, BRAND: 5}
        )
        collected.sort(key=lambda hit: priority.get(hit.source_type, 6))
        return collected[:6]

    @staticmethod
    def _verification_query(hotel_name: str, question: str) -> str:
        """Search query for one claim. Must stay property-agnostic: the claim's own
        words carry the specifics, the hint only adds the source vocabulary."""
        normalized = question.lower()
        if "farm" in normalized or "sourcing" in normalized or "produce" in normalized:
            hint = "farm sourcing suppliers ingredients official"
        elif "dinner" in normalized or "tasting" in normalized or "menu" in normalized:
            hint = "opening hours menu official"
        elif "english" in normalized or "reservation" in normalized:
            hint = "reservations official"
        elif "editorial" in normalized or "coverage" in normalized or "review" in normalized:
            hint = "travel magazine hospitality press"
        else:
            hint = ""
        words = [
            word for word in re.findall(r"[\w'’-]+", question)
            if word.lower() not in _QUERY_STOPWORDS
        ]
        return " ".join(part for part in (f'"{hotel_name}"', " ".join(words[:14]), hint) if part)

    def _assess_followup_claims(
        self, evidence_by_claim: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Classify each requested claim without turning missing evidence into a rejection."""
        prompt = (
            "Assess each claim independently using only the supplied evidence. Return a JSON array "
            "in the same order with: {\"claim\":\"\",\"status\":\"verified|unsupported|uncertain\"," 
            "\"finding\":\"\",\"source_urls\":[\"\"]}. VERIFIED means authoritative evidence "
            "affirmatively supports the claim. UNSUPPORTED means authoritative evidence directly "
            "contradicts the claim. Use UNCERTAIN when evidence is missing, blocked, ambiguous, only "
            "a weak snippet, or does not answer every part. Never turn absence of evidence into "
            "UNSUPPORTED. Do not alter or evaluate the editorial concept. Never invent URLs.\n\n"
            + json.dumps(evidence_by_claim, ensure_ascii=False)[:28000]
        )
        response = self._provider.complete(
            [LLMMessage(role="user", content=prompt)], system=load_prompt("researcher"),
            max_tokens=3000, temperature=0.05,
        )
        raw = extract_json_block(response.text)
        items = raw if isinstance(raw, list) else []
        results: list[dict[str, Any]] = []
        for index, bundle in enumerate(evidence_by_claim):
            item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
            status = str(item.get("status", "uncertain")).lower()
            if status not in {"verified", "unsupported", "uncertain"}:
                status = "uncertain"
            allowed = {
                str(source.get("url")): source for source in bundle.get("sources", [])
                if source.get("url")
            }
            source_urls = item.get("source_urls") if isinstance(item.get("source_urls"), list) else []
            sources = [allowed[url] for url in source_urls if url in allowed]
            if not sources and status != "uncertain":
                status = "uncertain"
            finding = str(item.get("finding") or "The available authoritative evidence does not establish this claim.")
            results.append({
                "claim": str(bundle.get("claim", "")),
                "status": status,
                "finding": finding,
                "sources": [{
                    "url": source.get("url", ""),
                    "title": source.get("title", ""),
                    "source_type": source.get("source_type", ""),
                } for source in sources],
            })
        return results
