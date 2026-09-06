# Treasury Indices Deployer

Standalone Index Finance treasury deployment tool for Robinhood Chain. This project is intentionally separate from the Pons launcher tools.

## Safety

- Running without arguments performs live read-only validation and simulation first.
- No transaction is broadcast unless you explicitly enter `y` or `yes`.
- Explicit execution also requires the `--broadcast` flag.
- The signing address is derived from `PRIVATE_KEY`.
- Private keys are loaded only from the gitignored `.env` and are never printed or stored in result files.
- Basket weights are generated automatically and always total 10,000 bps.

## Install on a VPS with uv

```bash
sudo apt-get update
sudo apt-get install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

cd ~/treasury-indices-deployer
uv sync --frozen
cp .env.example .env
cp treasury.config.example.json treasury.config.json
```

Edit `.env` and set your private key:

```dotenv
PRIVATE_KEY=0xYOUR_PRIVATE_KEY
RPC_URL=https://rpc.mainnet.chain.robinhood.com
```

Edit `treasury.config.json` if you want to change the asset catalog or advanced treasury settings.

## Interactive deployment

```bash
uv run python indices.py
```

The wizard:

1. Lists all configured assets with numbered choices.
2. Asks how many assets to include.
3. Accepts selections such as `1,5`, `1 5`, `1 and 5`, or `1 dan 5`.
4. Assigns equal weights that total exactly 100%.
5. Asks what percentage to distribute to holders, from `0` to `100` with up to two decimal places.
6. Offers epoch presets `15m`, `1h`, `6h`, and `24h`, while also accepting a custom compact duration.
7. Checks the Robinhood Chain ID, factory bytecode, and every selected asset contract.
8. Predicts the treasury address, simulates `deployTreasury`, estimates gas, and prints the maximum estimated cost.
9. Broadcasts only after `y` or `yes` confirmation.
10. Streams submitted and confirmed status, then stores the public result under `runs/`.

## Advanced CLI

Read-only preflight without exposing a private key:

```bash
uv run python indices.py preflight \
  --config treasury.config.json \
  --launcher 0xYOUR_PUBLIC_ADDRESS
```

Explicit live deployment:

```bash
uv run python indices.py execute \
  --config treasury.config.json \
  --broadcast
```

Explicit execution refuses to run without `--broadcast`.

## Configuration

Important fields in `treasury.config.json`:

- `factory_address`: Index Finance treasury factory.
- `treasury.creator`: `null` uses the signing wallet.
- `treasury.numeraire`: defaults to the zero address.
- `treasury.nonce`: `null` generates a fresh cryptographically random `uint96`.
- `treasury.asset_catalog`: reusable numbered asset list for interactive mode.
- `treasury.distribute_bps`: holder distribution percentage in basis points; `3750` means 37.50%.
- `treasury.epoch_length`: distribution interval in seconds; interactive mode offers `15m`, `1h`, `6h`, and `24h` presets.
- `treasury.assets` and `treasury.weights_bps`: basket used by explicit CLI modes.
- `gas_limit_multiplier_bps` and `max_fee_multiplier_bps`: configurable transaction safety buffers.

Result JSON includes the transaction hash, deployed treasury address, configuration nonce, and basket hash.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

Tests are offline and never broadcast transactions.
