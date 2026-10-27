# Образец SCC-файла для тестов

События с маркером сжатия: три свёрнутых (включая диапазон) и два свежих.

E0: (_SSC_) SYSTEM (роль): "You are a sandboxed test subject."
    ТОЧНЫЕ ДАННЫЕ:
    - НЕТ В ИСТОЧНИКЕ

E1: (_SSC_) USER (seq 8): TASK set-A part 1: прочитать 20 файлов
    ТОЧНЫЕ ДАННЫЕ:
    - setA/chain_000_kalpha000_n23.txt

E4–E13: (_SSC_) TOOL_RESULT (seq 26–35): прочитаны файлы 000–009
    ТОЧНЫЕ ДАННЫЕ:
    - setA/chain_000_kalpha000_n23.txt

E14: ASSISTANT (seq 38): свежее событие без маркера
    ТОЧНЫЕ ДАННЫЕ:
    - args={"file_path":"/data/setB/chain_500.txt"}

E15: USER (seq 69): ещё одно свежее событие
