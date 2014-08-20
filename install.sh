#!/bin/bash
BASE="$(dirname $0)"
DIR="$BASE"
FILES=$(ls -a "$DIR" | grep -E '^[.][^.]' \
                     | grep -vE '^.(hg|git)(ignore)?$' \
                     | grep -vE '^[.].*?[.]sw[op]$')

if [[ "${DIR:0:1}" != "/" ]]
then
    DIR="$(pwd)/$DIR"
fi

for file in $FILES
do
    echo "$BASE/$file" "->" "~/$file"
    ln -sfi "$DIR/$file" "$HOME/$file"
done
