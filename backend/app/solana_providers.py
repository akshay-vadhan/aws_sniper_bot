"""Helius-first Solana data access with Alchemy fallback.

Helius Enhanced returns parsed wallet transactions; Alchemy returns full RPC
transactions, which are normalized here to the fields the ingestion layer uses.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import settings

_provider_state = {"active": None, "last_fallback": None}


def configured_providers() -> list[str]:
    providers = []
    if settings.helius_api_key:
        providers.append("helius")
    if settings.alchemy_api_key:
        providers.append("alchemy")
    return providers


def _provider_error(provider: str, exc: Exception) -> RuntimeError:
    if isinstance(exc, httpx.HTTPStatusError):
        return RuntimeError(f"{provider} HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.RequestError):
        return RuntimeError(f"{provider} request failed ({type(exc).__name__})")
    return RuntimeError(f"{provider} returned an invalid response ({type(exc).__name__})")


async def _post_rpc(url: str, method: str, params: list[Any]) -> Any:
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(url, json={"jsonrpc": "2.0", "id": "radar", "method": method, "params": params})
        response.raise_for_status()
        payload = response.json()
    if payload.get("error"):
        error = payload["error"]
        raise RuntimeError(f"RPC {error.get('code', 'error')}")
    if "result" not in payload:
        raise RuntimeError("RPC response missing result")
    return payload["result"]


async def _helius_transactions(address: str) -> list[dict]:
    url = f"{settings.helius_enhanced_url.rstrip('/')}/v0/addresses/{address}/transactions"
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(url, params={"api-key": settings.helius_api_key, "limit": 100,
            "token-accounts": "balanceChanged", "commitment": "confirmed"})
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, list):
        raise RuntimeError("unexpected transactions response")
    return result


def _raw_amount(entry: dict | None) -> tuple[int, int]:
    if not entry:
        return 0, 0
    ui = entry.get("uiTokenAmount") or {}
    try:
        return int(ui.get("amount") or 0), int(ui.get("decimals") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _normalize_alchemy_transaction(row: dict, address: str) -> dict | None:
    transaction = row.get("transaction") or {}
    meta = row.get("meta") or {}
    signatures = transaction.get("signatures") or row.get("signatures") or []
    signature = row.get("signature") or (signatures[0] if signatures else None)
    if not signature:
        return None
    deltas: dict[str, dict[str, int]] = {}
    for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for balance in meta.get(field) or []:
            # Wallet-owned token accounts are identified by owner in RPC metadata.
            if balance.get("owner") != address:
                continue
            mint = balance.get("mint")
            if not mint:
                continue
            amount, decimals = _raw_amount(balance)
            bucket = deltas.setdefault(mint, {"amount": 0, "decimals": decimals})
            bucket["amount"] += sign * amount
    changes = [{"userAccount": address, "mint": mint,
                "rawTokenAmount": {"tokenAmount": str(item["amount"]), "decimals": item["decimals"]}}
               for mint, item in deltas.items() if item["amount"]]
    # Avoid counting token-account setup/close or transfers as swaps. A swap must
    # have both a non-quote token delta and a native/quote-side movement.
    keys = transaction.get("message", {}).get("accountKeys") or []
    if isinstance(keys, list):
        key_strings = [key.get("pubkey", "") if isinstance(key, dict) else str(key) for key in keys]
    else:
        key_strings = []
    account_index = key_strings.index(address) if address in key_strings else -1
    pre_lamports = (meta.get("preBalances") or [])
    post_lamports = (meta.get("postBalances") or [])
    sol_delta = 0
    if account_index >= 0 and account_index < len(pre_lamports) and account_index < len(post_lamports):
        # A wallet that only paid the transaction fee did not swap SOL. Add the
        # fee back when this wallet is the first (fee-payer) account key.
        fee = int(meta.get("fee") or 0) if account_index == 0 else 0
        sol_delta = post_lamports[account_index] - pre_lamports[account_index] + fee
    sol_changed = sol_delta != 0
    non_quote = [c for c in changes if c["mint"] not in {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}]
    if not non_quote:
        return {"signature": signature, "type": "TRANSFER", "timestamp": row.get("blockTime"),
                "tokenBalanceChanges": changes}
    # Token-to-token swaps may not change wallet SOL; a quote token delta can
    # serve as the second side. A lone token movement is treated as a transfer.
    has_quote_delta = any(c["mint"] in {"So11111111111111111111111111111111111111112",
                                       "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
                         for c in changes)
    tx_type = "SWAP" if sol_changed or has_quote_delta else "TRANSFER"
    return {"signature": signature, "type": tx_type, "timestamp": row.get("blockTime"),
            "tokenBalanceChanges": changes}


async def _alchemy_transactions(address: str) -> list[dict]:
    url = f"{settings.alchemy_solana_rpc_url.rstrip('/')}/{settings.alchemy_api_key}"
    result = await _post_rpc(url, "getTransactionsForAddress", [address, {
        "transactionDetails": "full", "sortOrder": "desc", "limit": 100,
        "commitment": "confirmed", "filters": {"status": "succeeded", "tokenAccounts": "balanceChanged"}}])
    rows = result if isinstance(result, list) else result.get("transactions", [])
    if not isinstance(rows, list):
        raise RuntimeError("unexpected transactions response")
    return [parsed for row in rows if isinstance(row, dict)
            if (parsed := _normalize_alchemy_transaction(row, address)) is not None]


async def fetch_wallet_transactions(address: str) -> list[dict]:
    providers = configured_providers()
    if not providers:
        raise RuntimeError("Configure HELIUS_API_KEY or ALCHEMY_API_KEY")
    failures = []
    for provider in providers:
        try:
            result = await (_helius_transactions(address) if provider == "helius"
                           else _alchemy_transactions(address))
            if _provider_state["active"] == "alchemy" and provider == "helius":
                _provider_state["last_fallback"] = None
            _provider_state["active"] = provider
            return result
        except Exception as exc:
            failures.append(_provider_error(provider, exc))
            if len(providers) > 1 and provider == providers[0]:
                _provider_state["last_fallback"] = provider
    raise RuntimeError("; ".join(str(error) for error in failures))


async def solana_rpc(method: str, params: list[Any]) -> Any:
    providers = configured_providers()
    if not providers:
        raise RuntimeError("Configure HELIUS_API_KEY or ALCHEMY_API_KEY")
    failures = []
    for provider in providers:
        if provider == "helius":
            url = f"{settings.helius_rpc_url.rstrip('/')}/?api-key={settings.helius_api_key}"
        else:
            url = f"{settings.alchemy_solana_rpc_url.rstrip('/')}/{settings.alchemy_api_key}"
        try:
            result = await _post_rpc(url, method, params)
            _provider_state["active"] = provider
            return result
        except Exception as exc:
            failures.append(_provider_error(provider, exc))
            if len(providers) > 1 and provider == providers[0]:
                _provider_state["last_fallback"] = provider
    raise RuntimeError("; ".join(str(error) for error in failures))


def provider_status() -> dict:
    providers = configured_providers()
    return {"configured": bool(providers), "configured_providers": providers,
            "active_provider": _provider_state["active"], "last_fallback_from": _provider_state["last_fallback"]}
