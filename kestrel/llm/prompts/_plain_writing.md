<!--
Shared plain-writing rules for every free-prose prompt (issue #93, D-UI-232).

Credit: the idea of a short, fixed set of writing rules plus a detect-before-rewrite check comes
from the open source `avoid-ai-writing` skill by Conor Bronsdon
(https://github.com/conorbronsdon/avoid-ai-writing, MIT license). Ideas only: no text, code or word
list is copied, nothing from that project is installed or loaded, and the wording below is ours.

The checker that enforces these rules is kestrel/writing_check.py. The word lists live in
config/writing_words.yml. Prompts include this block through the {PLAIN_WRITING} marker.
-->
Write plainly. Use only facts, numbers and names in the supplied material.
- No em or en dashes as punctuation. Use a period, comma or colon.
- No "not X, but Y" or "X was easy, Y was hard". Do not end on a lesson or moral; end on the result.
- Plain verbs (is, has, did). No "serves as", "stands as", "boasts".
- Never use: leverage (verb), robust, seamless, pivotal, delve, tapestry, landscape, journey, synergy, passionate, thrilled, unlock, elevate.
- No filler: "in order to", "it is important to note", "moreover", "furthermore", "could potentially".
- No groups of three unless the source has three.
- Do not invent feelings, dialogue or events. If a detail is missing, say so in the uncertain/question field.
