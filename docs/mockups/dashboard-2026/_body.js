/* The dashboard body, identical across all three variants — only the
   header treatment changes. Cash Flow Calendar is gone; Income by
   Category is new and sits directly under Expenses by Category. */
document.getElementById("dash-body").innerHTML = `
<div class="summary-row" id="summary-cards"></div>
<div class="dashboard-grid">

    <div class="card full-width">
        <div class="card-title">Monthly Overview</div>
        <div class="chart-container" style="height:350px"><canvas id="chart-monthly"></canvas></div>
    </div>

    <div class="card full-width">
        <div class="card-title">Expenses by Category</div>
        <div id="category-bars"></div>
    </div>

    <!-- NEW -->
    <div class="card full-width">
        <div class="card-title">Income by Category</div>
        <div id="income-bars"></div>
    </div>

    <div class="card full-width">
        <div class="flex items-center" style="justify-content:space-between;margin-bottom:14px">
            <div class="card-title" style="margin:0">Expense Trends</div>
            <button class="btn btn-secondary btn-sm">Top 5 categories</button>
        </div>
        <div class="chart-container"><canvas id="chart-trends"></canvas></div>
    </div>

    <div class="card full-width">
        <div class="flex items-center" style="justify-content:space-between;margin-bottom:14px">
            <div class="card-title" style="margin:0">Spending Heatmap</div>
            <div style="display:flex;align-items:center;gap:10px">
                <span id="heatmap-summary" style="font-size:12px;color:var(--text-tertiary)"></span>
                <select class="form-select" style="width:auto;min-width:100px;padding:4px 8px;font-size:12px"><option>2026</option></select>
            </div>
        </div>
        <div class="heatmap-wrap">
            <div class="heatmap-day-labels"><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span>Sun</span></div>
            <div class="heatmap-scroll">
                <div class="heatmap-month-labels" id="heatmap-month-labels"></div>
                <div class="heatmap-grid" id="heatmap-grid"></div>
            </div>
        </div>
        <div class="heatmap-legend">
            <span>Less</span>
            <span class="heatmap-cell" data-level="0"></span><span class="heatmap-cell" data-level="1"></span>
            <span class="heatmap-cell" data-level="2"></span><span class="heatmap-cell" data-level="3"></span>
            <span class="heatmap-cell" data-level="4"></span>
            <span>More</span>
        </div>
    </div>

    <!-- CHANGED: rows are clickable -->
    <div class="card full-width">
        <div class="flex items-center" style="justify-content:space-between;margin-bottom:16px">
            <div class="card-title" style="margin:0">Monthly Summary</div>
            <button class="btn btn-secondary btn-sm">Show Yearly</button>
        </div>
        <div class="table-container">
            <table id="summary-table">
                <thead id="summary-table-head"></thead>
                <tbody id="summary-table-body"></tbody>
            </table>
        </div>
    </div>

</div>`;
