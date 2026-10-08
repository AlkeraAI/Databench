----------------------------- MODULE commit_gc -----------------------------
(***************************************************************************)
(* A content commit racing the garbage sweep.                              *)
(*                                                                         *)
(* What this proves that a test cannot: the sweep decides what is garbage   *)
(* at one instant and acts on that decision later, while writers keep       *)
(* committing.  Two rules -- and only those two -- stand between that gap   *)
(* and a version pointing at bytes nobody has: an object under a session    *)
(* that was still open AT THE SCAN is a root, and an object written at or   *)
(* after the scan stamp is too young to move.  Deleting the age conjunct    *)
(* from ``gc_before_move`` violates ``NoDanglingReference``: an object is   *)
(* swept, expires, its key is written again by a new session, that session  *)
(* commits, and the sweep's stale candidate list moves the bytes out from   *)
(* under the ready version.  That counterexample is why this module exists, *)
(* and it is the behaviour the mutated-implementation twin of the trace     *)
(* replay reproduces against the code.                                     *)
(*                                                                         *)
(* THE CHECKPOINT SET THIS SPEC OWNS (README convention 3).  Each action is *)
(* named for the effect that just became visible; the implementation        *)
(* reaches the checkpoint of the same name with a dot for the first         *)
(* underscore.  The right column is the name section 5b.2 uses for the same *)
(* action, so the design doc and the module line up too.                    *)
(*                                                                         *)
(*   spec action                implementation             5b.2 name       *)
(*   ------------------------   ------------------------   -----------     *)
(*   content_after_store_put    cp.reach(                  Put             *)
(*                              "content.after_store_put")                 *)
(*                              -- after ObjectStore.put landed, before     *)
(*                              any row names the key                       *)
(*   content_before_commit      cp.reach(                  Commit          *)
(*                              "content.before_commit")                   *)
(*                              -- inside ContentService, after the         *)
(*                              re-hash, before the version row COMMITs     *)
(*   gc_after_reachability      cp.reach(                  SweepScan       *)
(*                              "gc.after_reachability")                   *)
(*                              -- Janitor.sweep, after the roots query      *)
(*   gc_before_move             cp.reach("gc.before_move")  Move (guard)    *)
(*                              -- after the candidate is confirmed and the *)
(*                              reference invalidated, before the store      *)
(*                              move                                        *)
(*   gc_after_move              cp.reach("gc.after_move")   Move (effect)   *)
(*                              -- the bytes are under deleted/             *)
(*                                                                         *)
(* Actions with NO checkpoint in the implementation today -- a review-      *)
(* visible gap, not a silent one.  The trace replay drives each through the *)
(* call named beside it:                                                    *)
(*                                                                         *)
(*   reference_invalidated      the trash purge dropping a version's        *)
(*                              reference (5b.2 Invalidate)                *)
(*   object_restored            Janitor restore, deleted/ -> live, before   *)
(*                              any head swap (5b.2 Restore)                *)
(*   object_expired             Janitor.expire_deleted past the window      *)
(*   session_crashed            an upload session abandoned mid-flight      *)
(*   sweep_crashed              the sweeper killed between the two move     *)
(*                              checkpoints -- nothing moved, the plan lost *)
(*   grant_opened, grant_closed, clock_tick -- the environment: an           *)
(*                              in-flight download grant and the injected    *)
(*                              Clock                                        *)
(*                                                                         *)
(* MODELLING ASSUMPTIONS, stated so a reviewer can disagree with them:      *)
(*                                                                         *)
(*  1. One shard, one sweep pass per behaviour.  A second scan recomputes   *)
(*     reachability from scratch, so it can only be safer than acting on    *)
(*     the first one's record; the dangerous gap is entirely inside a       *)
(*     single scan-then-move pass, and bounding it there keeps TLC under    *)
(*     the two-minute budget.                                               *)
(*  2. The move guard may read ONLY what the scan recorded (its reach set,  *)
(*     its candidate list, its stamp) plus the object's write time.  A      *)
(*     guard that re-read the live reference set would make                 *)
(*     NoDanglingReference true by construction and prove nothing -- and    *)
(*     the implementation does not re-read it either.                       *)
(*  3. Object identity is the store key.  An expired key can be written     *)
(*     again by a later session, which is what makes a stale candidate      *)
(*     list dangerous and what the age rule defends against.                *)
(*  4. A session that has begun committing is still a root: it is not done, *)
(*     and its object is about to be referenced.                            *)
(*  5. Time is a bounded counter; the state space, not the protocol, is     *)
(*     what stops at MaxClock.                                              *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Objects,     \* store keys under contention
    Sessions,    \* upload sessions: one that dies, one that re-writes a key
    NoSession,   \* the "nobody wrote this" sentinel; never a member of Sessions
    MaxClock,    \* state-space bound: the clock stops here
    Window       \* how long a deleted object stays recoverable before expiry

ASSUME NoSession \notin Sessions
ASSUME MaxClock \in Nat /\ Window \in Nat

ObjectStates == {"absent", "incoming", "live", "deleted"}
SessionStates == {"open", "committing", "done", "aborted"}

VARIABLES
    objState,     \* [Objects -> ObjectStates]
    owner,        \* which session wrote the key that is there now
    writtenAt,    \* when it was written -- the age rule reads this
    deletedAt,    \* when the sweep moved it -- the expiry window reads this
    sessState,    \* [Sessions -> SessionStates]
    refs,         \* objects a READY version references
    grants,       \* objects with an in-flight download grant (a root)
    clock,
    sweepPhase,   \* "idle" before a scan, "scanned" after it
    sweepStamp,   \* sweep_started_at, stamped once per pass
    sweepReach,   \* the roots the scan computed
    sweepCand,    \* the candidate list the scan produced
    moving        \* candidates whose reference is dropped, bytes not yet moved

vars == <<objState, owner, writtenAt, deletedAt, sessState, refs, grants,
          clock, sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

TypeOK ==
    /\ objState \in [Objects -> ObjectStates]
    /\ owner \in [Objects -> Sessions \cup {NoSession}]
    /\ writtenAt \in [Objects -> 0..MaxClock]
    /\ deletedAt \in [Objects -> 0..MaxClock]
    /\ sessState \in [Sessions -> SessionStates]
    /\ refs \subseteq Objects
    /\ grants \subseteq Objects
    /\ clock \in 0..MaxClock
    /\ sweepPhase \in {"idle", "scanned"}
    /\ sweepStamp \in 0..MaxClock
    /\ sweepReach \subseteq Objects
    /\ sweepCand \subseteq Objects
    /\ moving \subseteq Objects

(***************************************************************************)
(* Roots, exactly as the sweep's query computes them: every object a ready  *)
(* version names, every object an unfinished session owns, and every object *)
(* an in-flight download grant names.  Age is the fourth root and is not a  *)
(* set -- it is the per-object stamp comparison in Candidates below.        *)
(***************************************************************************)
Unfinished(s) == sessState[s] \in {"open", "committing"}

Roots ==
    refs
    \cup {o \in Objects : owner[o] # NoSession /\ Unfinished(owner[o])}
    \cup grants

Candidates(stamp) ==
    {o \in Objects :
        /\ objState[o] \in {"incoming", "live"}
        /\ o \notin Roots
        /\ writtenAt[o] < stamp}

Init ==
    /\ objState = [o \in Objects |-> "absent"]
    /\ owner = [o \in Objects |-> NoSession]
    /\ writtenAt = [o \in Objects |-> 0]
    /\ deletedAt = [o \in Objects |-> 0]
    /\ sessState = [s \in Sessions |-> "open"]
    /\ refs = {}
    /\ grants = {}
    /\ clock = 0
    /\ sweepPhase = "idle"
    /\ sweepStamp = 0
    /\ sweepReach = {}
    /\ sweepCand = {}
    /\ moving = {}

(***************************************************************************)
(* content.after_store_put -- the bytes are in the store under a key no row *)
(* names yet.  Create before reference: nothing points at it, and the only  *)
(* thing keeping it alive is its session.                                   *)
(***************************************************************************)
content_after_store_put(s, o) ==
    /\ sessState[s] = "open"
    /\ objState[o] = "absent"
    /\ objState' = [objState EXCEPT ![o] = "incoming"]
    /\ owner' = [owner EXCEPT ![o] = s]
    /\ writtenAt' = [writtenAt EXCEPT ![o] = clock]
    /\ UNCHANGED <<deletedAt, sessState, refs, grants, clock, sweepPhase,
                   sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* The session enters "committing": the version row is being written and is *)
(* not ready yet.  Still a root -- assumption 4.                            *)
(***************************************************************************)
content_committing(s, o) ==
    /\ sessState[s] = "open"
    /\ owner[o] = s
    /\ objState[o] = "incoming"
    /\ sessState' = [sessState EXCEPT ![s] = "committing"]
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, refs, grants, clock,
                   sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* content.before_commit -- the version becomes READY and its reference     *)
(* becomes visible in the same transaction that closes the session.         *)
(***************************************************************************)
content_before_commit(s, o) ==
    /\ sessState[s] = "committing"
    /\ owner[o] = s
    /\ objState[o] = "incoming"
    /\ objState' = [objState EXCEPT ![o] = "live"]
    /\ refs' = refs \cup {o}
    /\ sessState' = [sessState EXCEPT ![s] = "done"]
    /\ UNCHANGED <<owner, writtenAt, deletedAt, grants, clock, sweepPhase,
                   sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* A session abandoned mid-flight: the bytes stay, nothing references them, *)
(* and they stop being a root.  This is the only way an object becomes      *)
(* genuine garbage in this model.                                           *)
(***************************************************************************)
session_crashed(s) ==
    /\ Unfinished(s)
    /\ sessState' = [sessState EXCEPT ![s] = "aborted"]
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, refs, grants, clock,
                   sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* gc.after_reachability -- the stamp and the roots are taken ONCE, and     *)
(* everything the sweep does afterwards is decided from this record.        *)
(***************************************************************************)
gc_after_reachability ==
    /\ sweepPhase = "idle"
    /\ sweepStamp' = clock
    /\ sweepReach' = Roots
    /\ sweepCand' = Candidates(clock)
    /\ sweepPhase' = "scanned"
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   grants, clock, moving>>

(***************************************************************************)
(* reference_invalidated -- a trash purge drops a version's reference.  The *)
(* object keeps its bytes; it is now unreferenced, and a LATER scan may     *)
(* collect it.  The scan already taken must not.                            *)
(***************************************************************************)
reference_invalidated(o) ==
    /\ o \in refs
    /\ refs' = refs \ {o}
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, grants,
                   clock, sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* gc.before_move -- the guard.  It may consult ONLY the scan's record and  *)
(* the object's write time (assumption 2).  Both conjuncts are load-bearing:*)
(* drop the reachability one and an object under an open session is lost;   *)
(* drop the age one and a re-written key is moved out from under a ready    *)
(* version.                                                                 *)
(***************************************************************************)
gc_before_move(o) ==
    /\ sweepPhase = "scanned"
    /\ o \in sweepCand
    /\ o \notin sweepReach
    /\ writtenAt[o] < sweepStamp
    /\ objState[o] \in {"incoming", "live"}
    /\ o \notin moving
    /\ moving' = moving \cup {o}
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   grants, clock, sweepPhase, sweepStamp, sweepReach, sweepCand>>

(***************************************************************************)
(* gc.after_move -- the bytes are under deleted/, recoverable for Window.   *)
(***************************************************************************)
gc_after_move(o) ==
    /\ o \in moving
    /\ objState' = [objState EXCEPT ![o] = "deleted"]
    /\ deletedAt' = [deletedAt EXCEPT ![o] = clock]
    /\ moving' = moving \ {o}
    /\ UNCHANGED <<owner, writtenAt, sessState, refs, grants, clock,
                   sweepPhase, sweepStamp, sweepReach, sweepCand>>

(***************************************************************************)
(* The sweeper killed between the two checkpoints: the plan is lost and no  *)
(* bytes moved.  A redundant re-upload is the worst case -- the invariants  *)
(* must hold here exactly as they do on the completed path.                 *)
(***************************************************************************)
sweep_crashed ==
    /\ moving # {}
    /\ moving' = {}
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   grants, clock, sweepPhase, sweepStamp, sweepReach, sweepCand>>

(***************************************************************************)
(* object_restored -- deleted/ back to live before any head swap.           *)
(***************************************************************************)
object_restored(o) ==
    /\ objState[o] = "deleted"
    /\ objState' = [objState EXCEPT ![o] = "live"]
    /\ UNCHANGED <<owner, writtenAt, deletedAt, sessState, refs, grants, clock,
                   sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* object_expired -- past the window the bytes are gone and the key is free *)
(* to be written again.  That freedom is what makes a stale candidate list  *)
(* dangerous.                                                               *)
(***************************************************************************)
object_expired(o) ==
    /\ objState[o] = "deleted"
    /\ clock >= deletedAt[o] + Window
    /\ objState' = [objState EXCEPT ![o] = "absent"]
    /\ owner' = [owner EXCEPT ![o] = NoSession]
    /\ UNCHANGED <<writtenAt, deletedAt, sessState, refs, grants, clock,
                   sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

(***************************************************************************)
(* The environment.                                                        *)
(***************************************************************************)
grant_opened(o) ==
    /\ objState[o] = "live"
    /\ o \notin grants
    /\ grants' = grants \cup {o}
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   clock, sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

grant_closed(o) ==
    /\ o \in grants
    /\ grants' = grants \ {o}
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   clock, sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

clock_tick ==
    /\ clock < MaxClock
    /\ clock' = clock + 1
    /\ UNCHANGED <<objState, owner, writtenAt, deletedAt, sessState, refs,
                   grants, sweepPhase, sweepStamp, sweepReach, sweepCand, moving>>

Next ==
    \/ \E s \in Sessions :
         \/ session_crashed(s)
         \/ \E o \in Objects :
              \/ content_after_store_put(s, o)
              \/ content_committing(s, o)
              \/ content_before_commit(s, o)
    \/ \E o \in Objects :
         \/ reference_invalidated(o)
         \/ gc_before_move(o)
         \/ gc_after_move(o)
         \/ object_restored(o)
         \/ object_expired(o)
         \/ grant_opened(o)
         \/ grant_closed(o)
    \/ gc_after_reachability
    \/ sweep_crashed
    \/ clock_tick

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Invariants.                                                             *)
(***************************************************************************)

\* No version in state ready references an object that is absent or deleted.
\* This is the whole point: a reader following a ready version must find the
\* bytes.  The sweep is the only actor that can break it.
NoDanglingReference ==
    \A o \in refs : objState[o] \in {"incoming", "live"}

\* An object under a session that has not finished is never moved.  An upload
\* in flight is a root; sweeping it is a lost object, not a lost reference,
\* and no invariant on refs would catch it.
NoLostObject ==
    \A o \in Objects :
        (owner[o] # NoSession /\ Unfinished(owner[o])) => objState[o] # "deleted"

\* Bytes are never overwritten in place: a key holds nothing while a version
\* still points at it (section 5 invariant 3).
NoOverwriteInPlace ==
    \A o \in Objects : objState[o] = "absent" => o \notin refs

=============================================================================
