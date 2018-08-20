#!/bin/bash
BASE="$(dirname "$0")"
DIR="$BASE"
files="$(ls -a "$DIR")"
FILES=$( echo "$files" \
       | grep -E '^[.][^.]' \
       | grep -vE '^.(hg|git)(ignore)?$' \
       | grep -vE '^[.].*?[.]sw[op]$'
       )

if [[ "${DIR:0:1}" != "/" ]]
then
    DIR="$(pwd)/$DIR"
fi

for file in $FILES
do
    echo "$BASE/$file" "->" ~"/$file"
    ln -sfi "$DIR/$file" "$HOME/$file"
done
