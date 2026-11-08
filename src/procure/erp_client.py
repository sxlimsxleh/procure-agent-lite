"""Write draft POs to the (mock) SAP system: retries + idempotency (design rule 4).

Why both: retries make transient failures (503, 429, timeouts) survivable, but a retry after a
*lost response* would create a second PO. The Idempotency-Key makes the retry safe: the server
returns the PO it already created instead of creating another one.

The key is derived from the business facts, not random, so even a second click on "approve"
(a brand-new request from the client's point of view) maps to the same PO.
"""

import hashlib
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from procure.config import CustomerConfig
from procure.decide import Candidate
from procure.models import DraftPO, DraftPOItem, PurchaseRequest
from procure.normalise import CENT, UNIT_PRICE_PLACES, to_net

PO_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRY_AFTER_S = 60.0  # never let a server make us wait longer than this per attempt


class ErpError(Exception):
    """The ERP rejected the request (e.g. 400, 409). Retrying would not help."""


class RetryableError(ErpError):
    """Transient failure. Raised to the caller only after all attempts are used up."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MissingSupplierError(ValueError):
    """The winning supplier has no SAP supplier number: a human must create it first."""


def idempotency_key(pr_id: str, supplier_id: str, total_eur: Decimal) -> str:
    """sha256 of "PR-0001|100042|2230.00". The separator keeps ("PR-1", "23") and ("PR-12", "3")
    apart; the fixed 2 decimals keep 2230 and 2230.00 the same."""
    material = f"{pr_id}|{supplier_id}|{total_eur.quantize(CENT)}"
    return hashlib.sha256(material.encode()).hexdigest()


def build_draft_po(
    pr: PurchaseRequest, winner: Candidate, cfg: CustomerConfig
) -> tuple[DraftPO, str]:
    """Translate the recommendation into SAP's terms. Returns (draft PO, idempotency key).

    The PO is in the supplier's currency (as in SAP); the key uses the EUR total so it is stable.
    """
    quote = winner.quote
    supplier_id = cfg.supplier_id(quote.supplier_name)
    if supplier_id is None:
        raise MissingSupplierError(
            f"no SAP supplier number for {quote.supplier_name!r}; create the supplier in SAP first"
        )
    unit_price = quote.unit_price.value
    if unit_price is None or quote.vat_included is None or winner.total_eur is None:
        raise ValueError(f"{quote.quote_id} has no verified net price")  # decide() excludes these
    net_unit_price = to_net(unit_price, quote.vat_included, cfg.vat_rate).quantize(
        UNIT_PRICE_PLACES
    )
    draft = DraftPO(
        CompanyCode=pr.company_code,
        Supplier=supplier_id,
        DocumentCurrency=quote.currency,
        items=[
            DraftPOItem(
                Material=pr.material, OrderQuantity=pr.quantity, NetPriceAmount=net_unit_price
            )
        ],
    )
    return draft, idempotency_key(pr.pr_id, supplier_id, winner.total_eur)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


class ErpClient:
    def __init__(
        self,
        http: httpx.Client,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,  # injectable so tests do not really wait
        backoff_initial_s: float = 0.5,
        backoff_max_s: float = 8.0,
    ) -> None:
        self.http = http
        self.max_attempts = max_attempts
        self.sleep = sleep
        self._backoff = wait_exponential_jitter(multiplier=backoff_initial_s, max=backoff_max_s)
        self.attempts_last_call = 0

    def _wait(self, state: RetryCallState) -> float:
        """Honour Retry-After when the server sends one, otherwise exponential backoff + jitter."""
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER_S)
        return self._backoff(state)

    def _post_once(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        self.attempts_last_call += 1
        try:
            response = self.http.post(PO_PATH, json=payload, headers={"Idempotency-Key": key})
        except httpx.TransportError as exc:  # timeouts, connection resets, DNS, ...
            raise RetryableError(f"transport error: {exc!r}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableError(
                f"ERP returned {response.status_code}", _retry_after_seconds(response)
            )
        if response.status_code >= 400:
            raise ErpError(f"ERP returned {response.status_code}: {response.text}")
        return response.json()["d"]

    def create_po(self, draft: DraftPO, key: str) -> dict[str, Any]:
        """Create (or, on a replay, fetch) the PO. Returns the ERP's PO record."""
        payload = draft.model_dump(mode="json", by_alias=True)
        self.attempts_last_call = 0
        retrying = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type(RetryableError),
            sleep=self.sleep,
            reraise=True,  # surface our RetryableError, not tenacity's wrapper
        )
        return retrying(self._post_once, payload, key)

    def list_pos(self) -> list[dict[str, Any]]:
        response = self.http.get(PO_PATH)
        response.raise_for_status()
        return response.json()["d"]["results"]
