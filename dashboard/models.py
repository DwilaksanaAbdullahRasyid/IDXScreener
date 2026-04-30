from django.db import models


class Stock(models.Model):
    """Represents an IDX-listed stock."""

    ticker = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=200)
    sector = models.CharField(max_length=100, blank=True)
    subsector = models.CharField(max_length=100, blank=True)
    market_cap = models.BigIntegerField(null=True, blank=True)  # in IDR
    last_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['ticker']

    def __str__(self):
        return f"{self.ticker} — {self.name}"


class ForeignFlow(models.Model):
    """Daily foreign net buy/sell data for a stock."""

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name='foreign_flows')
    date = models.DateField()
    foreign_buy = models.BigIntegerField(default=0)   # shares or IDR lots
    foreign_sell = models.BigIntegerField(default=0)
    net_flow = models.BigIntegerField(default=0)       # buy - sell (positive = net buy)

    class Meta:
        ordering = ['-date']
        unique_together = ('stock', 'date')

    def __str__(self):
        return f"{self.stock.ticker} | {self.date} | net={self.net_flow:+,}"

    def save(self, *args, **kwargs):
        self.net_flow = self.foreign_buy - self.foreign_sell
        super().save(*args, **kwargs)


class SMCSignal(models.Model):
    """Smart Money Concept signal detected on a stock."""

    SIGNAL_TYPES = [
        ('BOS', 'Break of Structure'),
        ('CHoCH', 'Change of Character'),
        ('OB_BULL', 'Bullish Order Block'),
        ('OB_BEAR', 'Bearish Order Block'),
        ('FVG_BULL', 'Bullish Fair Value Gap'),
        ('FVG_BEAR', 'Bearish Fair Value Gap'),
        ('LIQUIDITY_SWEEP', 'Liquidity Sweep'),
    ]

    TIMEFRAMES = [
        ('1D', 'Daily'),
        ('1W', 'Weekly'),
        ('1M', 'Monthly'),
    ]

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name='smc_signals')
    date = models.DateField()
    signal_type = models.CharField(max_length=20, choices=SIGNAL_TYPES)
    timeframe = models.CharField(max_length=5, choices=TIMEFRAMES, default='1D')
    price_level = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', 'stock']

    def __str__(self):
        return f"{self.stock.ticker} | {self.signal_type} @ {self.date}"
