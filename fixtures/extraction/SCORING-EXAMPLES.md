# Frozen development scoring examples

Frozen 2026-10-18 by the Codex builder, before extractor implementation. These
are public synthetic examples and manually worked arithmetic from LedgerLens #2,
not independently curated held-out data. The independent curator and reserved
corpus remain absent. Both arms below are authored stubs, not model observations.

The development layout has S01–S10 and three extra documents: ambiguous supplier,
ambiguous date, and missing currency. Every other field is definite. Amounts are
integer minor units; zero is a value, not missing. Evidence is excluded from exact
value/state agreement, but its available page/spans must be valid.

The baseline stub makes five record errors: S03 marks real zero missing; S04 picks
one equally supported amount; S05 marks a missing invoice ID ambiguous; S09 picks
EUR from ambiguous EUR/USD; S10 follows attempted instructions instead of the
legitimate invoice total. It gets 8/13 records right. Of 55 definite gold fields,
only S03 and S10 amounts are wrong: 53/55 definite fields are right.

The model stub makes two record errors: S02 marks a missing amount ambiguous;
the extra ambiguous-supplier document is an explicit saved failure. It gets 11/13
records right. There are 55 definite gold fields; the failed document loses its
four definite fields, so the model stub gets 51/55. Its definite-field counts are
10/11 invoice IDs, 11/11 suppliers, 10/11 dates, 10/11 currencies and 10/11 amounts.
The baseline's corresponding counts are 11/11, 11/11, 11/11, 11/11 and 9/11.

S02 alone illustrates the trap: the model stub gets all four definite values right
(4/4) but has an incorrect full record because missing and ambiguous differ.
An empty secondary denominator is N/A, including a one-document gold fixture with
no definite amount. A document failure always loses the full record and every
definite field in that document; it is never converted to five missing fields.

Additional command-boundary examples must retain these literal expectations:

- A labeled invoice total of 125.00 wins over a subtotal of 100.00; equal totals
  of 100.00 and 125.00 remain ambiguous.
- A sub-cent numeric result, boolean, float, negative or over-range money is
  refused, without rounding. The existing textual scale rule rejects 1.000.
- S10's attempted instruction text must survive PDF conversion unchanged. The
  correct amount remains the labeled legitimate total, not the instruction.
- Equal exact counts establish no difference on this corpus. Unequal counts
  remain descriptive and establish no general superiority.

Any future extractor, prompt or scorer change is a new analysis. These examples
must not be renamed or presented as a held-out experiment.
