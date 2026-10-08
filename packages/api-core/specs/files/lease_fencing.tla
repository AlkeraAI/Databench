--------------------------- MODULE lease_fencing ---------------------------
(***************************************************************************)
(* Folder leases: acquire / heartbeat / reap / release, with epoch fencing. *)
(*                                                                         *)
(* What this proves that a test cannot: a holder that is paused past its    *)
(* TTL, reaped, and superseded by a second holder can still have a write    *)
(* in flight.  The write carries the epoch the holder believes it owns, and *)
(* the only thing between it and silent corruption is the ``FOR SHARE``     *)
(* re-read of the lease row inside the write's own transaction.  Removing   *)
(* that one conjunct from ``leases_fenced_write`` violates ``NoStaleWrite`` *)
(* in a handful of steps -- that counterexample is why this module exists.  *)
(*                                                                         *)
(* THE CHECKPOINT SET THIS SPEC OWNS (README convention 3).  Each action is *)
(* named for the effect that just became visible; the implementation        *)
(* reaches the checkpoint of the same name, namespaced ``leases.`` :        *)
(*                                                                         *)
(*   spec action              alkera_core/files/leases.py                   *)
(*   ---------------------    ------------------------------------------    *)
(*   leases_overlap_checked   cp.reach("leases.overlap_checked") -- the      *)
(*                            overlap probe inside LeaseService.acquire,     *)
(*                            between reading the covering lease and the     *)
(*                            grant statement                                *)
(*   leases_acquired          cp.reach("leases.acquired") -- after the       *)
(*                            INSERT ... ON CONFLICT DO UPDATE ... WHERE     *)
(*                            granted the epoch, before the hwm is raised    *)
(*   leases_final_applied     cp.reach("leases.final_applied") -- inside     *)
(*                            LeaseService.release, after the final snapshot *)
(*                            hook and before the release commits           *)
(*                                                                         *)
(* Actions with NO checkpoint in the implementation today -- a review-      *)
(* visible gap, not a silent one.  The trace-conformance replay drives each  *)
(* of these through the service call named beside it and asserts on the      *)
(* rows:                                                                    *)
(*                                                                         *)
(*   leases_heartbeat         LeaseService.heartbeat (one UPDATE ... WHERE   *)
(*                            epoch = $e AND holder_instance_id = $i)        *)
(*   leases_reaped            the reaper's lapse statement                   *)
(*   leases_fenced_write      assert_lease_epoch / fenced_write -- the       *)
(*                            ``FOR SHARE`` re-read                          *)
(*   platform_restored        the restore runbook bumping                    *)
(*                            file_platform.restore_generation               *)
(*   clock_tick, holder_paused, holder_resumed -- the environment: the       *)
(*                            injected Clock and a holder that stops beating *)
(*                                                                         *)
(* MODELLING ASSUMPTIONS, stated so a reviewer can disagree with them:      *)
(*                                                                         *)
(*  1. One lease row (one folder).  Two holders contend for it; the         *)
(*     implementation's per-node uniqueness makes every node independent.   *)
(*  2. Time is a bounded counter and expiry saturates at MaxClock, so the   *)
(*     finite model still exhibits "the clock eventually passes the TTL"    *)
(*     that liveness needs -- without it a bounded clock makes every        *)
(*     eventually-property vacuous at the bound.                            *)
(*  3. ``platform_restored`` rolls the lease row back to a RELEASED earlier *)
(*     state while the high-water mark and the restore generation persist.  *)
(*     That is the runbook: recovered leases are marked released and the    *)
(*     generation is bumped BEFORE the database is opened for writes.  A    *)
(*     restore that returned a *live* older row would let its holder write  *)
(*     behind a newer epoch, because assert_lease_epoch compares the epoch  *)
(*     to the row it finds and not to the generation floor.                 *)
(*  4. A holder keeps believing the epoch it was last granted (``belief``); *)
(*     that belief is what a fenced write carries, and nothing the holder   *)
(*     can observe locally revokes it.                                      *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Holders,     \* the set of contending holder instances (2 is enough)
    NoHolder,    \* the "no lease row" sentinel; never a member of Holders
    MaxEpoch,    \* state-space bound: no epoch above this is ever granted
    MaxClock,    \* state-space bound: the clock stops here, expiry saturates
    TTL,         \* how far past now() an acquire or heartbeat pushes expires
    Grace,       \* how long a reaped lease stays un-grantable
    GenStride,   \* the epoch floor a restore generation buys: gen * GenStride
    MaxGen,      \* state-space bound on restores
    MaxWrites    \* state-space bound on the landed-writes log

ASSUME NoHolder \notin Holders
ASSUME MaxEpoch \in Nat /\ MaxClock \in Nat /\ TTL \in Nat /\ Grace \in Nat
ASSUME GenStride \in Nat /\ MaxGen \in Nat /\ MaxWrites \in Nat

VARIABLES
    lease,          \* the file_leases row
    hwm,            \* file_lease_epoch_hwm: raised on every grant, survives a restore
    restoreGen,     \* file_platform.restore_generation
    clock,          \* the injected Clock
    paused,         \* holders that are not beating (a stopped world, a partition)
    belief,         \* the epoch each holder still thinks it owns
    writes,         \* the writes that LANDED, in order: the file_history log
    maxIssued,      \* ghost: the largest epoch ever granted, never rolled back
    epochRegressed  \* ghost: set when a grant fails to exceed every earlier one

vars == <<lease, hwm, restoreGen, clock, paused, belief, writes, maxIssued,
          epochRegressed>>

Max(a, b) == IF a >= b THEN a ELSE b
Min(a, b) == IF a =< b THEN a ELSE b
Max3(a, b, c) == Max(a, Max(b, c))

NoLease == [holder |-> NoHolder, epoch |-> 0, expires |-> 0,
            released |-> TRUE, grantableAfter |-> 0]

TypeOK ==
    /\ lease \in [holder: Holders \cup {NoHolder}, epoch: 0..MaxEpoch,
                  expires: 0..MaxClock, released: BOOLEAN,
                  grantableAfter: 0..MaxClock]
    /\ hwm \in 0..MaxEpoch
    /\ maxIssued \in 0..MaxEpoch
    /\ restoreGen \in 0..MaxGen
    /\ clock \in 0..MaxClock
    /\ paused \subseteq Holders
    /\ belief \in [Holders -> 0..MaxEpoch]
    /\ Len(writes) =< MaxWrites
    /\ epochRegressed \in BOOLEAN

(***************************************************************************)
(* The grant rule, spelled as the acquire statement's WHERE clause.         *)
(***************************************************************************)
Expired == lease.expires =< clock

CanAcquire ==
    /\ \/ lease.holder = NoHolder
       \/ lease.released
       \/ Expired
    /\ lease.grantableAfter =< clock

GenFloor == restoreGen * GenStride

\* GREATEST(coalesce(hwm, 0), restore_generation << 32, current epoch) + 1
NextEpoch == Max3(hwm, lease.epoch, GenFloor) + 1

\* The FOR SHARE re-read in assert_lease_epoch: the row must still name this
\* holder at this epoch, unreleased and unexpired, inside the write's own
\* transaction.  Deleting this predicate is the deliberately broken variant.
FenceOk(h) ==
    /\ lease.holder = h
    /\ lease.epoch = belief[h]
    /\ ~lease.released
    /\ ~Expired

Init ==
    /\ lease = NoLease
    /\ hwm = 0
    /\ maxIssued = 0
    /\ restoreGen = 0
    /\ clock = 0
    /\ paused = {}
    /\ belief = [h \in Holders |-> 0]
    /\ writes = <<>>
    /\ epochRegressed = FALSE

(***************************************************************************)
(* leases.overlap_checked -- the overlap probe.  It is a read: it refuses an *)
(* acquire a covering lease already owns and changes nothing else.  It is    *)
(* spelled as its own action so a replay can pause there.                    *)
(***************************************************************************)
leases_overlap_checked(h) ==
    /\ ~CanAcquire
    /\ lease.holder # h
    /\ UNCHANGED vars

(***************************************************************************)
(* leases.acquired -- one INSERT ... ON CONFLICT DO UPDATE ... WHERE.       *)
(***************************************************************************)
leases_acquired(h) ==
    /\ CanAcquire
    /\ NextEpoch =< MaxEpoch
    /\ lease' = [holder |-> h, epoch |-> NextEpoch,
                 expires |-> Min(clock + TTL, MaxClock), released |-> FALSE,
                 grantableAfter |-> clock]
    /\ hwm' = Max(hwm, NextEpoch)
    /\ belief' = [belief EXCEPT ![h] = NextEpoch]
    /\ maxIssued' = Max(maxIssued, NextEpoch)
    /\ epochRegressed' = (epochRegressed \/ NextEpoch =< maxIssued)
    /\ UNCHANGED <<restoreGen, clock, paused, writes>>

(***************************************************************************)
(* leases.heartbeat -- UPDATE ... WHERE epoch = $e AND holder = $i.  A      *)
(* paused holder takes no step; a superseded one matches zero rows, which   *)
(* is how it learns it lost the lease.  A lapsed-but-not-yet-reaped row is  *)
(* still beatable: that race is real and the model keeps it.                *)
(***************************************************************************)
leases_heartbeat(h) ==
    /\ h \notin paused
    /\ lease.holder = h
    /\ lease.epoch = belief[h]
    /\ ~lease.released
    /\ lease' = [lease EXCEPT !.expires = Min(clock + TTL, MaxClock)]
    /\ UNCHANGED <<hwm, restoreGen, clock, paused, belief, writes, maxIssued,
                   epochRegressed>>

(***************************************************************************)
(* leases.reaped -- the reaper lapses an expired lease, raises the hwm      *)
(* above the epoch it just retired, and holds the grant back for Grace.     *)
(***************************************************************************)
leases_reaped ==
    /\ lease.holder # NoHolder
    /\ ~lease.released
    /\ Expired
    /\ lease' = [lease EXCEPT !.released = TRUE,
                              !.grantableAfter = Min(clock + Grace, MaxClock)]
    /\ hwm' = Max(hwm, lease.epoch)
    /\ UNCHANGED <<restoreGen, clock, paused, belief, writes, maxIssued,
                   epochRegressed>>

(***************************************************************************)
(* leases.final_applied -- release: the final snapshot and the released_at  *)
(* stamp land in one transaction.                                          *)
(***************************************************************************)
leases_final_applied(h) ==
    /\ lease.holder = h
    /\ ~lease.released
    /\ lease.epoch = belief[h]
    /\ lease' = [lease EXCEPT !.released = TRUE, !.grantableAfter = clock]
    /\ hwm' = Max(hwm, lease.epoch)
    /\ UNCHANGED <<restoreGen, clock, paused, belief, writes, maxIssued,
                   epochRegressed>>

(***************************************************************************)
(* leases.fenced_write -- a holder submits a write at the epoch it believes *)
(* it owns.  It LANDS only if the lease row still matches.                  *)
(***************************************************************************)
leases_fenced_write(h) ==
    /\ Len(writes) < MaxWrites
    /\ belief[h] > 0
    /\ FenceOk(h)
    /\ writes' = Append(writes, [holder |-> h, epoch |-> belief[h]])
    /\ UNCHANGED <<lease, hwm, restoreGen, clock, paused, belief, maxIssued,
                   epochRegressed>>

(***************************************************************************)
(* The environment.                                                        *)
(***************************************************************************)
clock_tick ==
    /\ clock < MaxClock
    /\ clock' = clock + 1
    /\ UNCHANGED <<lease, hwm, restoreGen, paused, belief, writes, maxIssued,
                   epochRegressed>>

holder_paused(h) ==
    /\ h \notin paused
    /\ lease.holder = h
    /\ ~lease.released
    /\ paused' = paused \cup {h}
    /\ UNCHANGED <<lease, hwm, restoreGen, clock, belief, writes, maxIssued,
                   epochRegressed>>

holder_resumed(h) ==
    /\ h \in paused
    /\ paused' = paused \ {h}
    /\ UNCHANGED <<lease, hwm, restoreGen, clock, belief, writes, maxIssued,
                   epochRegressed>>

(***************************************************************************)
(* platform_restored -- the runbook.  The lease row goes back to an earlier *)
(* (released) state; hwm and the restore generation persist, and holders    *)
(* keep believing the epochs they were granted.  Monotonicity across this   *)
(* step is the whole point of the generation floor.                        *)
(***************************************************************************)
platform_restored ==
    /\ restoreGen < MaxGen
    /\ restoreGen' = restoreGen + 1
    /\ lease' = NoLease
    /\ UNCHANGED <<hwm, clock, paused, belief, writes, maxIssued,
                   epochRegressed>>

Next ==
    \/ \E h \in Holders :
         \/ leases_overlap_checked(h)
         \/ leases_acquired(h)
         \/ leases_heartbeat(h)
         \/ leases_final_applied(h)
         \/ leases_fenced_write(h)
         \/ holder_paused(h)
         \/ holder_resumed(h)
    \/ leases_reaped
    \/ clock_tick
    \/ platform_restored

Spec == Init /\ [][Next]_vars /\ WF_vars(clock_tick) /\ WF_vars(leases_reaped)

(***************************************************************************)
(* Invariants.                                                             *)
(***************************************************************************)

\* A holder may write only while the row still names it at its own epoch.
\* Two holders passing that fence at once is the corruption this design
\* exists to prevent.
SingleWriter == Cardinality({h \in Holders : FenceOk(h)}) =< 1

\* Every epoch ever granted is above every epoch granted before it -- across
\* a restore that lost the lease row.
EpochMonotonic == ~epochRegressed

\* No write at epoch e lands after a write at any epoch above e.
NoStaleWrite ==
    \A i \in 1..Len(writes) :
        \A j \in 1..Len(writes) :
            i < j => writes[i].epoch =< writes[j].epoch

(***************************************************************************)
(* Liveness: a lease never blocks the folder forever.  Under fairness of    *)
(* the clock and the reaper the lease is grantable again infinitely often   *)
(* -- a paused holder cannot hold a folder hostage.                         *)
(***************************************************************************)
EventuallyGrantable == []<>CanAcquire

=============================================================================
