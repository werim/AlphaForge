#DEFINE DB
DB="/Volumes/Slave/Projects/AlphaForge/data/runtime/alphaforge_runtime.db"
export DB

#PREFLIGHT BURNIN OPS
python -m alphaforge.burnin_ops \
>   --db "$DB" \
>   preflight \
>   --release-id phase9_trial_2 \
>   --symbols BTCUSDT,ETHUSDT \
>   --intervals 1h

#LAUNCH BURNIN OPS
python -m alphaforge.burnin_ops \
>   --db "$DB" \
>   launch \
>   --release-id phase9_trial_2 \
>   --duration-days 3 \
>   --symbols BTCUSDT,ETHUSDT \
>   --intervals 1h \
>   --detach \
>   --attach-timeout-seconds 60