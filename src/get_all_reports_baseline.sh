#!/usr/bin/env bash


module load opam/2.1.2 graphviz/2.49.0+py3.8.12 openmpi/4.1.3+cuda11.6.2
eval $(opam env)


python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --search-type=dfs --output=output/rl_results/DFS-search-report 

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --search-type=dfs --output=output/rl_results/DFS-search-report_128 --max-steps=128

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --search-type=dfs --output=output/rl_results/DFS-search-report_256 --max-steps=256

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --search-type=dfs --output=output/rl_results/DFS-search-report_512 --max-steps=512

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --search-type=dfs --output=output/rl_results/DFS-search-report_1024 --max-steps=1024

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --scoring-function=const --search-type=astar --output=output/rl_results/Astar-search-report  --max-steps=128

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --scoring-function=certainty --search-type=best-first --output=output/rl_results/BestFirst-search-report_cert  --max-steps=128

python ./src/search_file_cluster.py -p cpu --mem=8G --hard-depth-limit=20 --prelude=./CompCert --num-workers=32 --weightsfile=data/polyarg-weights-develop.dat -j1 compcert_projs_splits.json --scoring-function=const --search-type=best-first --output=output/rl_results/BestFirst-search-report_const  --max-steps=128