// ZenRippleAgentParent.sys.mjs — Minimal parent actor for ZenRippleAgent
// Required for actor registration; all logic lives in the child.

export class ZenRippleAgentParent extends JSWindowActorParent {
  receiveMessage(message) {
    // Parent receives no messages — all queries go parent→child via sendQuery.
  }
}
