"""
AutoBet Engine — SX.bet
Firma EIP712 y ejecuta automáticamente la contra-apuesta de cobertura.

Flujo:
  1. Bot detecta surebet (apuesta tuya con ROI >= MIN_ROI disponible)
  2. autobet.py busca la mejor orden disponible en el lado contrario
  3. Calcula el stake óptimo de cobertura
  4. Firma EIP712 con clave privada
  5. POST /orders/fill → apuesta ejecutada on-chain
  6. Notifica resultado por Telegram
"""

import logging
import os
import secrets
import time
from typing import Optional

import requests
from eth_account import Account
from web3 import Web3

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONSTANTES SX.BET (SX Rollup mainnet)
# ─────────────────────────────────────────────────────────────

CHAIN_ID          = 4162
API_BASE          = "https://api.sx.bet"
USDC_SCALE        = 1_000_000        # 1 USDC = 1,000,000 raw
ODDS_SCALE        = 10 ** 20
USDC_ADDRESS      = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
EIP712_FILL_HASHER = "0x3E96B0a25d51e3Cc89C557f152797c33B839968f"
DOMAIN_VERSION    = "5.0"

# Afiliado por defecto (dirección cero)
AFFILIATE_ADDRESS = "0x0000000000000000000000000000000000000000"

# Stake mínimo que acepta SX.bet (5 USDC ~ safe)
TAKER_MIN_USDC    = 5.0

# Reserva mínima que siempre dejar en la wallet (no gastar todo)
BALANCE_RESERVE   = 2.0   # USDC

# RPC de SX Rollup
SX_RPC            = "https://rpc.sx-rollup.gelato.digital"

# ABI mínimo USDC (balanceOf + allowance)
USDC_ABI = [
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner",   "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


# ─────────────────────────────────────────────────────────────
#  CLASE PRINCIPAL
# ─────────────────────────────────────────────────────────────

class AutoBetEngine:
    def __init__(self, api_key: str, private_key: str, wallet: str):
        self.api_key     = api_key
        self.private_key = private_key
        self.wallet      = wallet.strip()   # checksum format
        self.account     = Account.from_key(private_key)

        # Verificar que la clave corresponde a la wallet configurada
        derived = Web3.to_checksum_address(self.account.address)
        configured = Web3.to_checksum_address(self.wallet)
        if derived != configured:
            raise ValueError(
                f"⚠️ La clave privada no corresponde a la wallet configurada.\n"
                f"  Clave → {derived}\n"
                f"  SX_WALLET → {configured}"
            )

        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key":    api_key,
            "Content-Type": "application/json",
        })

        # Cache de metadata (salt del dominio)
        self._domain_salt: Optional[bytes] = None

    # ── Metadata ────────────────────────────────────────────

    def _get_domain_salt(self) -> bytes:
        """
        Devuelve el salt del dominio EIP712.
        SX.bet usa keccak256 del address del EIP712FillHasher como salt.
        Se cachea para no pedir metadata en cada firma.
        """
        if self._domain_salt is None:
            # Salt = keccak256(EIP712FillHasher address bytes)
            addr_bytes = bytes.fromhex(EIP712_FILL_HASHER[2:])  # 20 bytes
            self._domain_salt = Web3.keccak(addr_bytes)
        return self._domain_salt

    # ── Balance USDC ─────────────────────────────────────────

    def get_usdc_balance(self) -> float:
        """
        Lee el balance de USDC on-chain en SX Rollup.
        Retorna el importe en USDC (float). -1 si hay error.
        """
        try:
            w3   = Web3(Web3.HTTPProvider(SX_RPC, request_kwargs={"timeout": 8}))
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=USDC_ABI
            )
            raw = usdc.functions.balanceOf(
                Web3.to_checksum_address(self.wallet)
            ).call()
            return raw / USDC_SCALE
        except Exception as e:
            log.warning(f"get_usdc_balance error: {e}")
            return -1.0

    # ── Órdenes disponibles ──────────────────────────────────

    def get_best_orders(self, market_hash: str, want_outcome_one: bool, max_retries: int = 3) -> list:
        """
        Devuelve las mejores órdenes disponibles para apostar 'want_outcome_one'.
        Ordena por mejor cuota taker (mayor a menor).
        
        Si no hay órdenes, reintenta hasta max_retries veces con espera.
        """
        for attempt in range(max_retries):
            try:
                r = self.session.get(
                    f"{API_BASE}/orders",
                    params={"marketHashes": market_hash},
                    timeout=10
                )
                body = r.json()
                if body.get("status") != "success":
                    if attempt < max_retries - 1:
                        time.sleep(3 + attempt)  # 3s, 4s, 5s
                        continue
                    return []

                data = body.get("data", [])
                orders_flat = []
                if isinstance(data, list):
                    orders_flat = data
                elif isinstance(data, dict):
                    for arr in data.values():
                        if isinstance(arr, list):
                            orders_flat.extend(arr)

                # Para apostar outcome_one como TAKER, necesito makers en outcome_two (y viceversa)
                # Un maker que hace outcome_one → yo como taker hago outcome_two, y al revés
                matching = []
                for o in orders_flat:
                    maker_side = bool(o.get("isMakerBettingOutcomeOne"))
                    if maker_side == want_outcome_one:
                        continue  # mismo lado, no aplica
                    try:
                        pct = float(o["percentageOdds"]) / ODDS_SCALE
                        if 0 < pct < 1:
                            taker_odds = 1.0 / (1.0 - pct)
                            fillable   = float(o.get("fillAmount", 0)) / USDC_SCALE
                            if fillable > 0:
                                matching.append({
                                    "orderHash":    o["orderHash"],
                                    "market_hash":  o.get("marketHash", market_hash),
                                    "taker_odds":   taker_odds,
                                    "pct_odds_raw": int(o["percentageOdds"]),
                                    "fillable_usdc": fillable,
                                    "outcome_one":  want_outcome_one,
                                })
                    except Exception:
                        pass

                # Si encontramos órdenes, retornar
                if matching:
                    matching.sort(key=lambda x: x["taker_odds"], reverse=True)
                    log.info(f"Found {len(matching)} orders on attempt {attempt+1}")
                    return matching
                
                # No hay órdenes, reintentar
                if attempt < max_retries - 1:
                    log.info(f"No orders found, retrying in {3+attempt}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(3 + attempt)

            except Exception as e:
                log.error(f"get_best_orders error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(3 + attempt)
                    continue
                return []
        
        log.warning(f"No orders found after {max_retries} attempts")
        return []

    # ── Firma EIP712 ─────────────────────────────────────────

    def _sign_fill(
        self,
        order_hash: str,
        taker_amount_raw: int,
        pct_odds_raw: int,
        market_hash: str,
        betting_outcome_one: bool,
        fill_salt_hex: str,
    ) -> str:
        """Firma el fill order y devuelve la firma hex."""
        domain_salt = self._get_domain_salt()

        domain = {
            "name":    "FillOrderSportX",
            "version": DOMAIN_VERSION,
            "chainId": CHAIN_ID,
            "salt":    "0x" + domain_salt.hex(),
        }
        types = {
            "Details": [
                {"name": "action",    "type": "string"},
                {"name": "market",    "type": "string"},
                {"name": "betting",   "type": "string"},
                {"name": "stake",     "type": "string"},
                {"name": "odds",      "type": "string"},
                {"name": "orderHash", "type": "string"},
                {"name": "fillSalt",  "type": "bytes32"},
            ],
        }
        message = {
            "action":    "N/A",
            "market":    market_hash,
            "betting":   "true" if betting_outcome_one else "false",
            "stake":     str(taker_amount_raw),
            "odds":      str(pct_odds_raw),
            "orderHash": order_hash,
            "fillSalt":  "0x" + fill_salt_hex,
        }

        signed = self.account.sign_typed_data(
            domain_data   = domain,
            message_types = types,
            message_data  = message,
        )
        return "0x" + signed.signature.hex()

    # ── Ejecutar apuesta ────────────────────────────────────

    def place_hedge(
        self,
        market_hash:        str,
        betting_outcome_one: bool,
        hedge_stake_usdc:   float,
        min_odds:           float,
    ) -> dict:
        """
        Ejecuta la contra-apuesta de cobertura.

        Args:
            market_hash:         hash del mercado
            betting_outcome_one: True si apostamos por outcomeOne
            hedge_stake_usdc:    USDC a apostar (cobertura calculada)
            min_odds:            cuota mínima aceptable (decimal)

        Returns:
            dict con 'success', 'message', y detalles del fill
        """
        if hedge_stake_usdc < TAKER_MIN_USDC:
            return {
                "success": False,
                "message": f"Stake de cobertura ({hedge_stake_usdc:.2f} USDC) "
                           f"está por debajo del mínimo ({TAKER_MIN_USDC} USDC)."
            }

        # 0. Verificar balance disponible
        balance = self.get_usdc_balance()
        if balance < 0:
            log.warning("No se pudo verificar balance — continuando igualmente")
        else:
            available = balance - BALANCE_RESERVE
            if available < TAKER_MIN_USDC:
                return {
                    "success": False,
                    "message": f"Saldo insuficiente: {balance:.2f} USDC en wallet "
                               f"(reserva {BALANCE_RESERVE:.0f} USDC → disponible {available:.2f} USDC).",
                    "balance": balance,
                }
            if hedge_stake_usdc > available:
                log.info(
                    f"Stake de cobertura reducido: {hedge_stake_usdc:.2f} → {available:.2f} USDC "
                    f"(balance {balance:.2f} - reserva {BALANCE_RESERVE:.0f})"
                )
                hedge_stake_usdc = available

        # 1. Obtener mejores órdenes disponibles (con reintentos)
        orders = self.get_best_orders(market_hash, betting_outcome_one, max_retries=3)
        if not orders:
            return {
                "success": False,
                "message": "No hay órdenes disponibles después de 3 intentos (orderbook vacío en live)."
            }

        # 2. Filtrar por cuota mínima
        valid = [o for o in orders if o["taker_odds"] >= min_odds]
        if not valid:
            best_available = orders[0]["taker_odds"] if orders else 0
            return {
                "success": False,
                "message": f"Cuota disponible ({best_available:.3f}) menor que la mínima requerida ({min_odds:.3f})."
            }

        # 3. Elegir la mejor orden que tenga suficiente liquidez
        best = valid[0]
        # Limitar al fillable de la orden si es menor que lo que queremos
        actual_stake = min(hedge_stake_usdc, best["fillable_usdc"])
        if actual_stake < TAKER_MIN_USDC:
            return {
                "success": False,
                "message": f"Liquidez insuficiente en la orden (solo {best['fillable_usdc']:.2f} USDC disponibles)."
            }

        # Avisar si hubo reducción de stake respecto al ideal
        stake_reduced = actual_stake < hedge_stake_usdc - 0.01

        # 4. Convertir a raw
        taker_amount_raw = int(actual_stake * USDC_SCALE)
        fill_salt_hex    = secrets.token_hex(32)   # 32 bytes aleatorios

        log.info(
            f"AutoBet: {market_hash[:12]}… "
            f"side={'O1' if betting_outcome_one else 'O2'} "
            f"stake={actual_stake:.2f} USDC @ {best['taker_odds']:.3f}"
        )

        # 5. Firmar
        try:
            taker_sig = self._sign_fill(
                order_hash          = best["orderHash"],
                taker_amount_raw    = taker_amount_raw,
                pct_odds_raw        = best["pct_odds_raw"],
                market_hash         = market_hash,
                betting_outcome_one = betting_outcome_one,
                fill_salt_hex       = fill_salt_hex,
            )
        except Exception as e:
            return {"success": False, "message": f"Error en firma EIP712: {e}"}

        # 6. POST /orders/fill
        payload = {
            "orderHashes":       [best["orderHash"]],
            "takerAmounts":      [str(taker_amount_raw)],
            "taker":             self.wallet,
            "takerSig":          taker_sig,
            "fillSalt":          fill_salt_hex if not fill_salt_hex.startswith("0x") else fill_salt_hex,
            "action":            "N/A",
            "market":            market_hash,
            "betting":           "true" if betting_outcome_one else "false",
            "bettingOutcomeOne": str(betting_outcome_one).lower(),
            "affiliateAddress":  AFFILIATE_ADDRESS,
        }

        try:
            r = self.session.post(
                f"{API_BASE}/orders/fill",
                json=payload,
                timeout=15
            )
            body = r.json()
            log.info(f"AutoBet response: {body}")

            if r.status_code == 200 and body.get("status") == "success":
                msg = "✅ Contra-apuesta ejecutada correctamente."
                if stake_reduced:
                    msg += f" ⚠️ Stake reducido por saldo limitado (ideal: {hedge_stake_usdc:.2f} USDC)."
                return {
                    "success":       True,
                    "message":       msg,
                    "stake":         actual_stake,
                    "odds":          best["taker_odds"],
                    "order_hash":    best["orderHash"],
                    "stake_reduced": stake_reduced,
                    "http_status":   r.status_code,
                }
            else:
                err = body.get("message") or body.get("error") or str(body)
                return {
                    "success":     False,
                    "message":     f"API rechazó el fill: {err}",
                    "http_status": r.status_code,
                    "raw":         body,
                }

        except Exception as e:
            return {"success": False, "message": f"Error de red: {e}"}

    # ── Verificar aprobación USDC ────────────────────────────

    def check_usdc_approved(self) -> bool:
        """
        Verifica si el TokenTransferProxy tiene allowance de USDC.
        Si no, la primera apuesta automática fallará.
        El usuario debe haber apostado manualmente al menos una vez (eso aprueba automáticamente).
        """
        from web3 import Web3
        try:
            w3 = Web3(Web3.HTTPProvider("https://rpc.sx-rollup.gelato.digital"))
            # ABI mínimo para allowance
            usdc_abi = [{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                         "name":"allowance","outputs":[{"name":"","type":"uint256"}],
                         "stateMutability":"view","type":"function"}]
            # TokenTransferProxy del metadata
            token_transfer_proxy = "0xCc4fBba7D0E0F2A03113F42f5D3aE80d9B2aD55d"
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=usdc_abi
            )
            allowance = usdc.functions.allowance(
                Web3.to_checksum_address(self.wallet),
                Web3.to_checksum_address(token_transfer_proxy)
            ).call()
            log.info(f"USDC allowance: {allowance / USDC_SCALE:.2f} USDC")
            return allowance > 0
        except Exception as e:
            log.warning(f"No se pudo verificar allowance: {e}")
            return True  # Asumir aprobado si no se puede verificar
