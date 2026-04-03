"""
Correlation-Aware Sector Limits — prevents over-concentration in correlated assets.

Loads sector groups from config/sector_groups.yaml and checks whether a new
position would exceed the group's max_positions limit.
"""

import os

import yaml

from src.config import app_config
from src.logger import log


# Module-level cache
_sector_config = None
_symbol_to_group = {}


def _load_sector_groups():
    """Loads sector group config from YAML and builds reverse lookup."""
    global _sector_config

    cfg = app_config.get('settings', {}).get('sector_limits', {})
    config_file = cfg.get('config_file', 'config/sector_groups.yaml')

    # Resolve relative to project root
    if not os.path.isabs(config_file):
        project_root = os.path.join(os.path.dirname(__file__), '..', '..')
        config_file = os.path.join(project_root, config_file)

    try:
        with open(config_file, 'r') as f:
            _sector_config = yaml.safe_load(f)
    except FileNotFoundError:
        log.warning(f"Sector groups config not found: {config_file}")
        _sector_config = {'default_max_positions_per_group': 2, 'groups': {}}
        return
    except Exception as e:
        log.error(f"Failed to load sector groups: {e}")
        _sector_config = {'default_max_positions_per_group': 2, 'groups': {}}
        return

    # Build reverse lookup: symbol → group name
    _symbol_to_group.clear()
    groups = _sector_config.get('groups', {})
    for group_name, group_data in groups.items():
        symbols = group_data.get('symbols', [])
        for sym in symbols:
            sym_str = str(sym).upper()
            if sym_str in _symbol_to_group:
                log.warning(f"Symbol {sym_str} in multiple groups: "
                            f"{_symbol_to_group[sym_str]} and {group_name}. "
                            f"Using first: {_symbol_to_group[sym_str]}")
            else:
                _symbol_to_group[sym_str] = group_name

    log.info(f"Loaded {len(groups)} sector groups with "
             f"{len(_symbol_to_group)} symbol mappings.")


def _ensure_loaded():
    """Loads config on first access."""
    if _sector_config is None:
        _load_sector_groups()


def reload_sector_groups():
    """Forces reload of YAML. For tests and hot-reload."""
    global _sector_config, _symbol_to_group
    _sector_config = None
    _symbol_to_group = {}
    _load_sector_groups()


def get_symbol_group(symbol: str) -> str | None:
    """Returns group name for a symbol, or None if ungrouped."""
    _ensure_loaded()
    return _symbol_to_group.get(symbol.upper())


def get_group_symbols(group_name: str) -> list[str]:
    """Returns list of symbols in a sector group, or empty list if not found."""
    _ensure_loaded()
    if not _sector_config:
        return []
    group_data = _sector_config.get('groups', {}).get(group_name, {})
    return [str(s).upper() for s in group_data.get('symbols', [])]


def get_group_limit(group_name: str) -> int:
    """Returns max_positions for a group."""
    _ensure_loaded()
    if not _sector_config:
        return 2
    groups = _sector_config.get('groups', {})
    group_data = groups.get(group_name, {})
    return group_data.get(
        'max_positions',
        _sector_config.get('default_max_positions_per_group', 2)
    )


def check_sector_limit(symbol: str, open_positions: list,
                        trading_strategy: str = 'manual') -> tuple:
    """Check if opening a position in `symbol` would exceed its sector group limit.

    Args:
        symbol: The ticker to check.
        open_positions: List of dicts with at least 'symbol' and 'status' keys.
        trading_strategy: 'manual' or 'auto' (for logging context).

    Returns:
        (allowed: bool, reason: str)
    """
    cfg = app_config.get('settings', {}).get('sector_limits', {})
    if not cfg.get('enabled', True):
        return True, ""

    _ensure_loaded()

    # --- Asset class limit (cross-group concentration check) ---
    ac_result = _check_asset_class_limit(symbol, open_positions)
    if not ac_result[0]:
        return ac_result

    group = get_symbol_group(symbol)
    if group is None:
        # Ungrouped symbol — use default limit
        default_limit = _sector_config.get('default_max_positions_per_group', 2) if _sector_config else 2
        # Count ungrouped open positions
        ungrouped_count = sum(
            1 for p in open_positions
            if p.get('status') == 'OPEN' and get_symbol_group(p.get('symbol', '')) is None
        )
        if ungrouped_count >= default_limit:
            return False, f"Ungrouped limit ({default_limit}) reached ({ungrouped_count} open)"
        return True, ""

    limit = get_group_limit(group)

    # Count open positions in the same group
    group_count = sum(
        1 for p in open_positions
        if p.get('status') == 'OPEN' and get_symbol_group(p.get('symbol', '')) == group
    )

    if group_count >= limit:
        return False, (f"Sector group '{group}' at limit: "
                       f"{group_count}/{limit} positions open")

    return True, ""


def _check_asset_class_limit(symbol: str, open_positions: list) -> tuple:
    """Check cross-group asset class concentration limit.

    Returns (allowed: bool, reason: str).
    """
    if not _sector_config:
        return True, ""

    ac_limits = _sector_config.get('asset_class_limits', {})
    if not ac_limits:
        return True, ""

    # Determine asset class of the symbol being checked
    # Crypto groups are the first 9, but we use a simpler heuristic:
    # if the symbol has an asset_type from positions, use that;
    # otherwise infer from sector group names
    asset_class = _infer_asset_class(symbol)
    if not asset_class:
        return True, ""

    ac_cfg = ac_limits.get(asset_class, {})
    max_pos = ac_cfg.get('max_positions')
    if max_pos is None:
        return True, ""

    # Count open positions in same asset class
    class_count = sum(
        1 for p in open_positions
        if p.get('status') == 'OPEN' and _infer_asset_class(p.get('symbol', '')) == asset_class
    )

    if class_count >= max_pos:
        return False, (f"Asset class '{asset_class}' at limit: "
                       f"{class_count}/{max_pos} positions open")

    return True, ""


# Crypto sector group names (used to infer asset class from group)
_CRYPTO_GROUPS = frozenset([
    'l1_major', 'l1_legacy', 'defi', 'l2_scaling',
    'ai_data', 'depin', 'rwa', 'gaming', 'meme', 'infra',
    'liquid_staking', 'privacy', 'btc_ecosystem', 'misc_alt',
])


def _infer_asset_class(symbol: str) -> str | None:
    """Infer asset class ('crypto' or 'stock') from the symbol's sector group."""
    group = get_symbol_group(symbol)
    if group is None:
        return None
    if group in _CRYPTO_GROUPS:
        return 'crypto'
    return 'stock'


def get_asset_class_concentration(symbol: str, open_positions: list) -> float:
    """Returns a position size multiplier (0.0 to 1.0) based on concentration.

    When concentration_scaling is enabled for the asset class, returns a
    reduced multiplier as the number of positions approaches the limit.
    """
    _ensure_loaded()
    if not _sector_config:
        return 1.0

    ac_limits = _sector_config.get('asset_class_limits', {})
    asset_class = _infer_asset_class(symbol)
    if not asset_class:
        return 1.0

    ac_cfg = ac_limits.get(asset_class, {})
    if not ac_cfg.get('concentration_scaling', False):
        return 1.0

    max_pos = ac_cfg.get('max_positions')
    if not max_pos or max_pos <= 1:
        return 1.0

    class_count = sum(
        1 for p in open_positions
        if p.get('status') == 'OPEN' and _infer_asset_class(p.get('symbol', '')) == asset_class
    )

    # Linear scale-down: at 0 positions → 1.0, at max-1 → 1/max
    # Example with max=4: 0→1.0, 1→0.75, 2→0.50, 3→0.25
    remaining_slots = max_pos - class_count
    multiplier = remaining_slots / max_pos
    return max(0.1, multiplier)  # Floor at 10% to avoid near-zero positions


def get_sector_exposure_summary(open_positions: list) -> dict:
    """Returns current sector exposure for the /sectors Telegram command.

    Returns:
        dict of {group_name: {current: int, limit: int, symbols: list[str]}}
    """
    _ensure_loaded()

    summary = {}
    if not _sector_config:
        return summary

    groups = _sector_config.get('groups', {})

    # Initialize all groups
    for group_name in groups:
        summary[group_name] = {
            'current': 0,
            'limit': get_group_limit(group_name),
            'symbols': [],
        }

    # Count open positions per group
    ungrouped_symbols = []
    for pos in open_positions:
        if pos.get('status') != 'OPEN':
            continue
        sym = pos.get('symbol', '')
        group = get_symbol_group(sym)
        if group and group in summary:
            summary[group]['current'] += 1
            summary[group]['symbols'].append(sym)
        elif group is None:
            ungrouped_symbols.append(sym)

    # Add ungrouped bucket if any
    if ungrouped_symbols:
        default_limit = _sector_config.get('default_max_positions_per_group', 2)
        summary['_ungrouped'] = {
            'current': len(ungrouped_symbols),
            'limit': default_limit,
            'symbols': ungrouped_symbols,
        }

    # Filter to only groups with open positions (for concise display)
    return {k: v for k, v in summary.items() if v['current'] > 0}
