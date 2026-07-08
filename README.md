# twitterAFF — X & Threads Automation

Auto-reply, auto-post, auto-comment bot for X (Twitter) and Threads.net.

## Structure
```
scripts/
├── auto_reply.py              # X: scan + reply pipeline
├── multi_account_scan.py      # X: orchestrator (runs all accounts)
├── auto_approve_pending.py    # X: auto-approve pending replies
├── auto_post.py               # X: post reply to X
├── viral_research.py          # X: scrape trending content
├── viral_research_grouped.py  # X: grouped niche research
├── link_categorizer.py        # X: categorize links from product KB
├── threads_post.py            # Threads: post content
├── threads_auto_comment.py    # Threads: auto-comment on trending posts
├── daily_content.py           # Threads: generate daily content (LLM)
└── threads_orchestrator.py    # Threads: orchestrator
templates/
├── persona.example.json       # Account persona template
└── link_knowledge.example.json# Link/product knowledge base template
.env.example                   # Environment variables template
```
