LOST & FOUND AI — FINAL FUNCTIONAL UPDATE

This build keeps the dashboard interface with:
- Report Lost Item
- Lost News Feed
- Report Found Item
- Found News Feed
- Algorithm Dashboard
- Evaluation
- My Profile

Functional fixes:
1. Lost reports appear only in Lost News Feed.
2. Found reports appear only in Found News Feed.
3. Public feeds show fictional reporter names, but NOT contact numbers or status.
4. Pending/found status and contact information are private and visible in My Profile only.
5. My Profile is linked to the browser's local user ID and automatically includes newly submitted reports.
6. Lost submissions search found reports; found submissions search lost reports.
7. Possible matches show a similarity percentage, reporter name, and contact information.
8. If no candidate reaches the normal threshold, the strongest non-zero candidates are still shown as WEAK / POSSIBLE matches.
9. Clicking a possible-match card opens the Algorithm Dashboard and its latest trace/results.
10. Evaluation runs automatically after a successful search and its metrics are saved for the latest run.
11. Sample posts are seeded into BOTH Lost and Found feeds independently with fictional names and demo contact numbers.

Run:
1. Extract the whole folder.
2. Run run_windows.bat.
3. Open http://127.0.0.1:5000
4. Do not open the HTML directly or use VS Code Live Server.
