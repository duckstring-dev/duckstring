# Clustering benchmark, TPC-DS SF100

DuckDB 1.5.6, 10 threads, memory limit 25.5 GiB, macOS-26.6.2-arm64-arm-64bit-Mach-O. Queries: median of 5 warm runs after 1 warm-up.

## Set `keys`: ss_sold_date_sk, ss_item_sk, ss_customer_sk

287,997,024 rows, 5 bits per column.

| Layout | Stats (s) | Key (s) | Write (s) | Size (GB) |
|---|---|---|---|---|
| generated | 0.0 | 0.0 | 27.6 | 14.41 |
| hash | 0.0 | 0.5 | 192.0 | 21.09 |
| lexicographic | 0.0 | 0.0 | 158.3 | 18.08 |
| morton | 1.2 | 5.0 | 177.9 | 19.29 |
| hilbert | 1.2 | 12.4 | 212.7 | 19.24 |
| rank_morton | failed: OutOfMemoryException: Out of Memory Error: failed to offload data block of size 128.0 KiB (60.0 GiB/60.0 GiB used). | | | |
| qrank_morton | 24.3 | 16.1 | 224.3 | 19.30 |
| qrank_hilbert | 24.7 | 27.1 | 205.2 | 19.25 |
| arank_morton | 4.8 | 7.1 | 180.5 | 19.30 |
| arank_hilbert | 5.1 | 11.0 | 166.4 | 19.25 |

### Share of rows in row groups read (min/max pruning)

| Query | Matched | generated | hash | lexicographic | morton | hilbert | qrank_morton | qrank_hilbert | arank_morton | arank_hilbert |
|---|---|---|---|---|---|---|---|---|---|---|
| point ss_customer_sk | 0.000% | 100.0% | 100.0% | 100.0% | 14.0% | 8.9% | 14.0% | 9.0% | 14.0% | 9.1% |
| point ss_item_sk | 0.001% | 100.0% | 100.0% | 73.7% | 11.2% | 8.9% | 11.6% | 8.8% | 11.6% | 8.7% |
| point ss_sold_date_sk | 0.082% | 100.0% | 100.0% | 0.1% | 9.7% | 9.9% | 8.9% | 9.1% | 9.0% | 9.3% |
| box all 0.01% | 0.010% | 100.0% | 100.0% | 3.8% | 0.5% | 0.2% | 0.5% | 0.3% | 0.5% | 0.2% |
| box all 0.10% | 0.093% | 100.0% | 100.0% | 9.4% | 0.8% | 0.5% | 0.7% | 0.6% | 0.8% | 0.5% |
| box all 1.00% | 0.931% | 100.0% | 100.0% | 19.6% | 3.9% | 2.9% | 4.1% | 3.1% | 4.1% | 3.1% |
| range ss_customer_sk 0.1% | 0.093% | 100.0% | 100.0% | 100.0% | 16.9% | 10.7% | 17.0% | 10.9% | 17.0% | 9.4% |
| range ss_customer_sk 1.0% | 0.946% | 100.0% | 100.0% | 100.0% | 16.5% | 10.2% | 16.4% | 10.3% | 16.7% | 10.4% |
| range ss_customer_sk 10.0% | 9.578% | 100.0% | 100.0% | 100.0% | 25.1% | 19.9% | 25.1% | 20.1% | 25.1% | 20.2% |
| range ss_item_sk 0.1% | 0.093% | 100.0% | 100.0% | 77.1% | 12.0% | 9.2% | 12.6% | 9.3% | 12.4% | 9.0% |
| range ss_item_sk 1.0% | 1.015% | 100.0% | 100.0% | 79.2% | 11.6% | 9.3% | 12.1% | 9.5% | 12.1% | 9.4% |
| range ss_item_sk 10.0% | 9.987% | 100.0% | 100.0% | 86.5% | 22.1% | 20.4% | 22.4% | 20.5% | 22.5% | 20.4% |
| range ss_sold_date_sk 0.1% | 0.155% | 100.0% | 100.0% | 0.2% | 9.7% | 9.7% | 8.9% | 9.4% | 9.0% | 9.5% |
| range ss_sold_date_sk 1.0% | 0.992% | 100.0% | 100.0% | 1.0% | 8.4% | 8.6% | 8.9% | 9.3% | 8.8% | 9.4% |
| range ss_sold_date_sk 10.0% | 9.604% | 100.0% | 100.0% | 9.7% | 21.4% | 21.5% | 18.0% | 18.4% | 17.8% | 18.4% |
| join_category | 2.515% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| join_customer | 0.113% | 100.0% | 100.0% | 100.0% | 99.1% | 98.7% | 99.1% | 98.7% | 99.2% | 98.5% |
| join_month | 3.056% | 100.0% | 100.0% | 3.1% | 18.8% | 18.9% | 8.9% | 9.5% | 9.1% | 9.2% |
| join_quarter | 2.746% | 100.0% | 100.0% | 2.8% | 9.5% | 9.2% | 12.7% | 12.8% | 12.7% | 12.6% |

### Query time, ms (mean over instances of the median)

| Query | Matched | generated | hash | lexicographic | morton | hilbert | qrank_morton | qrank_hilbert | arank_morton | arank_hilbert |
|---|---|---|---|---|---|---|---|---|---|---|
| point ss_customer_sk | 0.000% | 19 | 411 | 185 | 53 | 36 | 42 | 37 | 44 | 39 |
| point ss_item_sk | 0.001% | 271 | 274 | 203 | 24 | 22 | 25 | 29 | 25 | 31 |
| point ss_sold_date_sk | 0.082% | 228 | 265 | 14 | 29 | 29 | 27 | 28 | 27 | 28 |
| box all 0.01% | 0.010% | 662 | 970 | 40 | 24 | 20 | 18 | 21 | 18 | 22 |
| box all 0.10% | 0.093% | 737 | 1026 | 70 | 24 | 20 | 19 | 22 | 20 | 22 |
| box all 1.00% | 0.931% | 783 | 1058 | 136 | 44 | 32 | 32 | 33 | 34 | 35 |
| range ss_customer_sk 0.1% | 0.093% | 246 | 596 | 416 | 85 | 50 | 62 | 52 | 60 | 45 |
| range ss_customer_sk 1.0% | 0.946% | 298 | 624 | 447 | 84 | 44 | 62 | 53 | 62 | 57 |
| range ss_customer_sk 10.0% | 9.578% | 328 | 636 | 480 | 133 | 90 | 101 | 97 | 96 | 101 |
| range ss_item_sk 0.1% | 0.093% | 381 | 395 | 239 | 33 | 28 | 33 | 35 | 32 | 36 |
| range ss_item_sk 1.0% | 1.015% | 390 | 424 | 247 | 45 | 31 | 36 | 43 | 34 | 40 |
| range ss_item_sk 10.0% | 9.987% | 402 | 458 | 278 | 79 | 58 | 61 | 69 | 60 | 71 |
| range ss_sold_date_sk 0.1% | 0.155% | 227 | 263 | 15 | 27 | 27 | 24 | 27 | 26 | 27 |
| range ss_sold_date_sk 1.0% | 0.992% | 266 | 272 | 17 | 30 | 27 | 28 | 33 | 29 | 33 |
| range ss_sold_date_sk 10.0% | 9.604% | 290 | 302 | 31 | 61 | 53 | 47 | 59 | 48 | 65 |
| join_category | 2.515% | 500 | 538 | 478 | 426 | 327 | 303 | 353 | 305 | 364 |
| join_customer | 0.113% | 481 | 720 | 627 | 632 | 482 | 446 | 516 | 437 | 526 |
| join_month | 3.056% | 431 | 451 | 42 | 85 | 66 | 44 | 52 | 47 | 56 |
| join_quarter | 2.746% | 428 | 455 | 39 | 59 | 47 | 50 | 59 | 52 | 63 |

## Set `skewed`: ss_sold_date_sk, ss_net_paid

287,997,024 rows, 7 bits per column.

| Layout | Stats (s) | Key (s) | Write (s) | Size (GB) |
|---|---|---|---|---|
| generated | 0.0 | 0.0 | 31.4 | 14.41 |
| hash | 0.0 | 0.5 | 213.4 | 21.09 |
| lexicographic | 0.0 | 0.0 | 161.9 | 17.81 |
| morton | 0.8 | 3.8 | 180.5 | 18.65 |
| hilbert | 0.8 | 9.5 | 214.4 | 18.49 |
| rank_morton | failed: OutOfMemoryException: Out of Memory Error: failed to offload data block of size 96.0 KiB (59.9 GiB/60.0 GiB used). | | | |
| qrank_morton | 44.7 | 68.9 | 211.6 | 18.88 |
| qrank_hilbert | 49.0 | 88.1 | 255.2 | 18.74 |
| arank_morton | 3.9 | 15.0 | 195.1 | 18.88 |
| arank_hilbert | 4.2 | 18.4 | 198.5 | 18.74 |

### Share of rows in row groups read (min/max pruning)

| Query | Matched | generated | hash | lexicographic | morton | hilbert | qrank_morton | qrank_hilbert | arank_morton | arank_hilbert |
|---|---|---|---|---|---|---|---|---|---|---|
| point ss_net_paid | 0.227% | 100.0% | 100.0% | 77.8% | 12.6% | 9.3% | 4.1% | 2.4% | 4.1% | 2.5% |
| point ss_sold_date_sk | 0.082% | 100.0% | 100.0% | 0.1% | 2.5% | 2.3% | 2.9% | 2.6% | 3.0% | 2.5% |
| box all 0.01% | 0.010% | 100.0% | 100.0% | 0.8% | 0.4% | 0.3% | 0.3% | 0.1% | 0.2% | 0.1% |
| box all 0.10% | 0.097% | 100.0% | 100.0% | 2.8% | 0.8% | 0.6% | 0.5% | 0.4% | 0.5% | 0.3% |
| box all 1.00% | 0.938% | 100.0% | 100.0% | 8.6% | 2.3% | 1.9% | 1.8% | 1.6% | 1.9% | 1.6% |
| range ss_net_paid 0.1% | 0.093% | 100.0% | 100.0% | 79.7% | 15.2% | 11.3% | 4.1% | 2.5% | 4.1% | 2.5% |
| range ss_net_paid 1.0% | 0.945% | 100.0% | 100.0% | 82.5% | 16.2% | 11.9% | 4.9% | 3.5% | 4.9% | 3.5% |
| range ss_net_paid 10.0% | 9.592% | 100.0% | 100.0% | 81.4% | 22.5% | 19.2% | 13.5% | 12.3% | 13.5% | 12.3% |
| range ss_sold_date_sk 0.1% | 0.155% | 100.0% | 100.0% | 0.2% | 2.5% | 2.2% | 3.3% | 2.9% | 3.2% | 2.8% |
| range ss_sold_date_sk 1.0% | 0.992% | 100.0% | 100.0% | 1.0% | 2.6% | 2.3% | 4.6% | 4.1% | 4.6% | 4.1% |
| range ss_sold_date_sk 10.0% | 9.604% | 100.0% | 100.0% | 9.7% | 11.9% | 11.8% | 12.5% | 12.1% | 12.7% | 12.2% |
| join_category | 2.515% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| join_customer | 0.113% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| join_month | 3.056% | 100.0% | 100.0% | 3.1% | 5.3% | 5.1% | 6.1% | 5.5% | 5.9% | 5.4% |
| join_quarter | 2.746% | 100.0% | 100.0% | 2.8% | 3.7% | 3.6% | 6.2% | 5.9% | 6.2% | 5.7% |

### Query time, ms (mean over instances of the median)

| Query | Matched | generated | hash | lexicographic | morton | hilbert | qrank_morton | qrank_hilbert | arank_morton | arank_hilbert |
|---|---|---|---|---|---|---|---|---|---|---|
| point ss_net_paid | 0.227% | 214 | 166 | 86 | 29 | 19 | 15 | 14 | 18 | 16 |
| point ss_sold_date_sk | 0.082% | 300 | 262 | 15 | 17 | 16 | 16 | 16 | 19 | 19 |
| box all 0.01% | 0.010% | 362 | 287 | 19 | 19 | 15 | 20 | 14 | 16 | 16 |
| box all 0.10% | 0.097% | 387 | 307 | 21 | 19 | 15 | 21 | 14 | 17 | 16 |
| box all 1.00% | 0.938% | 399 | 329 | 28 | 22 | 18 | 23 | 17 | 20 | 19 |
| range ss_net_paid 0.1% | 0.093% | 255 | 195 | 105 | 33 | 22 | 16 | 15 | 18 | 17 |
| range ss_net_paid 1.0% | 0.945% | 260 | 199 | 109 | 35 | 24 | 20 | 16 | 20 | 19 |
| range ss_net_paid 10.0% | 9.592% | 264 | 205 | 107 | 54 | 41 | 33 | 25 | 32 | 28 |
| range ss_sold_date_sk 0.1% | 0.155% | 293 | 261 | 14 | 16 | 15 | 16 | 15 | 18 | 17 |
| range ss_sold_date_sk 1.0% | 0.992% | 349 | 272 | 15 | 18 | 16 | 19 | 17 | 21 | 18 |
| range ss_sold_date_sk 10.0% | 9.604% | 380 | 302 | 25 | 33 | 28 | 29 | 26 | 35 | 30 |
| join_category | 2.515% | 569 | 460 | 414 | 410 | 350 | 478 | 337 | 433 | 375 |
| join_customer | 0.113% | 552 | 631 | 539 | 593 | 462 | 651 | 469 | 571 | 525 |
| join_month | 3.056% | 494 | 383 | 33 | 38 | 33 | 42 | 30 | 37 | 32 |
| join_quarter | 2.746% | 502 | 396 | 31 | 35 | 29 | 44 | 31 | 40 | 34 |
