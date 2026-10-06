# Proj-SemWeb
Link download data (1 file CSV): https://zenodo.org/records/4265096

Yêu cầu của thầy:
1. Define an ontology for the selected domain -> output là file `ontology.ttl`
2. Collect relevant data in this domain -> là bộ data CSV trên zenodo
3. Transform collected data into 4* standard -> Code trong dir `pipeline`, tự chạy lại sẽ ra file `book_data.ttl` (require: file data CSV + file `ontology.ttl`)
4. Find and establish links to other datasets to obtain 5* standard -> Code trong dir `linking`, chạy ra file `books_5star.ttl` (là `book_data.ttl` nhưng thêm external links) và `external_links.ttl` (chỉ có external links)
5. Provide an interface via SPARQL endpoint/termnal to query data -> Chạy code trong dir `sparql-endpoint` (cần có 2 file: `ontology.ttl` và `books_5star.ttl`)

GG Drive chứa các file .ttl cho 5 bước trên: https://drive.google.com/drive/folders/1T3HZWRmZwJLTeaUYWYYMibKD5bon05Xq?usp=sharing
