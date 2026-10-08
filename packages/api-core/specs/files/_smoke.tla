---------------------------- MODULE _smoke ----------------------------
(* A bounded counter, here only to prove the model-checking pipeline end to
   end: `make files-specs` fetches the jar, picks a runtime and reports a
   verdict. The invariant is deliberately trivial and deliberately
   falsifiable -- the tooling test mutates InRange and requires TLC to go
   red, because a runner that always exits 0 is worse than none. *)
EXTENDS Naturals

CONSTANT Limit

VARIABLE n

Init == n = 0

Tick == /\ n < Limit
        /\ n' = n + 1

Idle == /\ n = Limit
        /\ UNCHANGED n

Next == Tick \/ Idle

Spec == Init /\ [][Next]_n

InRange == n \in 0..Limit

=======================================================================
