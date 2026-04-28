# Pluto — Consent, Disclaimer & Liability

## Disclaimer

Pluto is provided **as-is**, without warranty of any kind — express or implied — including but not limited to warranties of merchantability, fitness for a particular purpose, or non-infringement.

**The maintainers and developers of this repository bear no responsibility or liability** of any kind — direct, indirect, incidental, special, exemplary, or consequential — for any damages, losses, security incidents, data corruption, system damage, or any other harm arising from the use, misuse, or inability to use this software.

## User Responsibility

**You, the user, are solely and fully responsible for:**

- Any harm, damage, data loss, or security incident caused by running Pluto or any of its components
- Granting consent to AI agents to receive injected messages via PlutoAgentFriend
- Running automated injections into agent input streams
- Exposing the Pluto server or message bus to networks you do not fully control
- Coordinating AI agents that take destructive, irreversible, or harmful actions on your systems
- Any unauthorized, unintended, or negligent usage of Pluto

By using Pluto you accept full responsibility for the above. If you do not accept these terms, do not use this software.

## Purpose & Positive Intent

Pluto is built with entirely **positive intentions**, solely for **research and development** in legitimate multi-agent AI coordination scenarios. The maintainers and developers of Pluto have **no malicious intent**. This project exists for beneficial, experimental, and research purposes only.

### Code Injection Is a Powerful Action

The core feature of `PlutoAgentFriend` — writing messages directly into an AI agent's input stream (stdin via PTY) — is a **powerful and potentially dangerous capability**. You must:

1. **Carefully inspect** what Pluto injects before enabling automated mode.
2. **Review the source code** if you have any doubt about what the tool does.
3. **Use only in environments you own and control.**
4. **Never point agents at production systems, shared infrastructure, or sensitive data** unless you fully understand and accept the risks.

## Consent for Agent Injection

When you run `PlutoAgentFriend`, you are explicitly authorizing Pluto to inject text into your AI agent's input stream. This consent:

- Applies only to the current session.
- Is revoked when you terminate the wrapper (`Ctrl-C` or closing the terminal).
- Does **not** authorize the injected content to bypass the agent's own safety rules — the agent continues applying its normal judgement to every request.

### Copilot CLI — Injection Not Supported

GitHub Copilot CLI enforces an internal **safe layer** that rejects unsolicited text injected into its input stream. This restriction applies **even when the user has explicitly given consent**. Therefore:

- `PlutoAgentFriend` **cannot inject prompts into Copilot CLI**.
- Automated agent coordination via injection is **not available** for Copilot.
- All communication with a Copilot-based agent must use **PlutoClient** with active polling from the agent side.

See [docs/guide/pluto-agent-friend.md](docs/guide/pluto-agent-friend.md) for details and the recommended polling pattern.

## Third-Party Agent CLIs

Pluto wraps third-party AI agent CLIs (Claude Code, GitHub Copilot CLI, Aider, Cursor, etc.). These tools are governed by their own terms of service, safety policies, and usage restrictions. The Pluto maintainers are not affiliated with these tools and make no guarantees about compatibility, ongoing support, or compliance with their terms. You are responsible for ensuring your use of Pluto complies with the terms of any third-party tool you wrap.

## No Warranty

THIS SOFTWARE IS PROVIDED "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE, ARE DISCLAIMED. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

---

See also: [README.md](README.md) · [docs/guide/pluto-agent-friend.md](docs/guide/pluto-agent-friend.md)
