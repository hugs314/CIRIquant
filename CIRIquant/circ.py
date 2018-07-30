#! /usr/bin/env python
# -*- encoding:utf-8 -*-
import os
import sys
import re
import logging
import pysam
import subprocess
import simplejson as json
from multiprocessing import Pool
from collections import defaultdict
from itertools import izip_longest

LOGGER = logging.getLogger('CIRIquant')
PREFIX = re.compile(r'(.+)[/_-][12]')


class BedParser(object):
    """
    Class for parsing circRNA information in bed file
    """


    def __init__(self, content):
        self.chr = content[0]
        self.start = int(content[1])
        self.end = int(content[2])
        self.circ_id = content[3]
        self.strand = content[5]
        self.length = self.end - self.start + 1


def load_bed(fname):
    """
    Load Back-Spliced Junction Sites in bed file

    Parameters
    -----
    fname : str
        input file name

    Returns
    -----
    dict
        divide all circRNAs into different chromsomes

    """
    circ_info = defaultdict(dict)
    with open(fname, 'r') as f:
        for line in f:
            content = line.rstrip().split('\t')
            parser = BedParser(content)
            circ_info[parser.chr][parser.circ_id] = parser
    return circ_info


def load_fai(fname):
    """
    Load fai index of fasta created by samtools

    Parameters
    -----
    fname : str
        input fai file name

    Returns
    -----
    dict
         chromsome and start / end position in file

    """
    faidx = {}
    with open(fname, 'r') as f:
        for line in f:
            content = line.rstrip().split('\t')
            chrom, length, start, eff_length, line_length = content
            shift_length = int(length) * int(line_length) / int(eff_length)
            faidx[chrom] = [int(start), shift_length]
    return faidx


def extract_seq(fasta, start, length):
    """
    Extract sequence from fasta according to given position

    Parameters
    -----
    fasta : str
        file name of fasta
    start : int
        offset of chrosome
    length : int
        length of chromsome sequence

    Returns
    -----
    str
        sequence from start to start + length

    """
    with open(fasta, 'r') as f:
        f.seek(start, 0)
        seq = f.read(length)
        seq = re.sub('\n', '', seq)
    return seq


def generate_index(log_file, circ_info, config, circ_fasta):
    """
    Generate pseudo circular index

    Parameters
    -----
    log_file : str
        file name of log file, used for subprocess.PIPE
    circ_info : dict
        back-spliced junction sites
    config : dict
        config informations
    circ_fasta :
        output fasta file name

    Returns
    -----
    dict
        chromosomes used in BSJ sites

    """

    from logger import ProgressBar

    fai = config['genome'] + '.fai'
    if not os.path.exists(fai):
        LOGGER.debug('Indexing FASTA')
        index_cmd = '{} faidx {}'.format(config['samtools'], config['genome'])
        with open(log_file, 'a') as log:
            subprocess.call(index_cmd, shell=True, stderr=log, stdout=log)
    fasta_index = load_fai(fai)

    LOGGER.info('Extract circular sequence')
    prog = ProgressBar()
    prog.update(0)
    cnt = 0
    with open(circ_fasta, 'w') as out:
        for chrom in sorted(circ_info.keys()):
            prog.update(100 * cnt / len(circ_info))
            cnt += 1
            if chrom not in fasta_index:
                sys.exit('Unconsistent chromosome id: {}'.format(chrom))
            chrom_start, chrom_length = fasta_index[chrom]
            chrom_seq = extract_seq(config['genome'], chrom_start, chrom_length)

            chrom_circ = circ_info[chrom]
            for circ_id in chrom_circ:
                parser = chrom_circ[circ_id]
                circ_seq = chrom_seq[parser.start - 1:parser.end] * 2
                if circ_seq.count('N') > len(circ_seq) * 0.5 or len(circ_seq) == 0:
                    continue
                out.write('>{}'.format(parser.circ_id) + '\n')
                out.write(circ_seq + '\n')
    prog.update(100)

    return fasta_index


def build_index(log_file, thread, pseudo_fasta, outdir, prefix, config):
    """
    Build hisat2 index for pseudo circular index

    Returns
    -----
    str
        index file name used in denovo mapping

    """
    LOGGER.info('Building circular index ..')
    denovo_index = '{}/circ/{}_index'.format(outdir, prefix)

    build_cmd = '{}-build -p {} -f {} {}'.format(
        config['hisat2'],
        thread,
        pseudo_fasta,
        denovo_index
    )

    with open(log_file, 'a') as log:
        subprocess.call(build_cmd, shell=True, stderr=log, stdout=log)

    return denovo_index


def denovo_alignment(log_file, thread, reads, outdir, prefix, config):
    """
    Call hisat2 to read re-alignment

    Returns
    -----
    str
        Output bam file

    """
    LOGGER.info('De novo alignment for circular RNAs ..')
    denovo_bam = '{}/circ/{}_denovo.bam'.format(outdir, prefix)
    sorted_bam = '{}/circ/{}_denovo.sorted.bam'.format(outdir, prefix)

    align_cmd = '{} -p {} --dta -q -x {}/circ/{}_index -1 {} -2 {} | {} view -bS > {}'.format(
        config['hisat2'],
        thread,
        outdir,
        prefix,
        reads[0],
        reads[1],
        config['samtools'],
        denovo_bam,
    )

    sort_cmd = '{} sort --threads {} -o {} {}'.format(
        config['samtools'],
        thread,
        sorted_bam,
        denovo_bam,
    )

    index_cmd = '{} index -@ {} {}'.format(
        config['samtools'],
        thread,
        sorted_bam,
    )

    with open(log_file, 'a') as log:
        subprocess.call(align_cmd, shell=True, stderr=log, stdout=log)
        subprocess.call(sort_cmd, shell=True, stderr=log, stdout=log)
        subprocess.call(index_cmd, shell=True, stderr=log, stdout=log)

    return sorted_bam


def grouper(iterable, n, fillvalue=None):
    """
    Collect data info fixed-length chunks or blocks
    grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx
    """

    args = [iter(iterable)] * n
    return izip_longest(*args, fillvalue=None)


def proc_denovo_bam(bam_file, thread, threshold):
    """
    Extract BSJ reads in denovo mapped bam file

    Returns
    -----
    dict
        query_name -> mate_id -> pysam.AlignSegment

    """

    LOGGER.info('Detecting reads containing Back-splicing signals')
    sam = pysam.AlignmentFile(bam_file, 'rb')

    header = sam.header['SQ']
    sam.close()

    pool = Pool(thread, denovo_initializer, (bam_file, threshold, ))
    jobs = []
    chunk_size = max(500, len(header) / threshold + 1)
    for circ_chunk in grouper(header, chunk_size):
        jobs.append(pool.apply_async(denovo_worker, (circ_chunk, )))
    pool.close()
    pool.join()

    cand_reads = defaultdict(dict)
    for job in jobs:
        tmp_cand = job.get()
        for read_id, mate_id, circ_id in tmp_cand:
            cand_reads[read_id][mate_id] = circ_id

    return cand_reads


BAM = None
THRESHOLD = None
def denovo_initializer(infile, threshold):
    """
    Initializer for passing bam file name
    """
    global BAM, THRESHOLD
    BAM, THRESHOLD = infile, threshold


def denovo_worker(circ_chunk):
    """
    Find candidate reads with junction signal

    Parameters
    -----
    circ_chunk : list
        list of Pysam header to process

    Returns
    -----
    list
        pysam.AlignedSegment, candidate reads with junction signal

    """
    sam = pysam.AlignmentFile(BAM, 'rb')
    cand_reads = []
    for d in circ_chunk:
        if d is None:
            continue
        circ_id, junc_site = d['SN'], int(d['LN']) / 2
        for read in sam.fetch(circ_id, multiple_iterators=True):
            if read.is_unmapped or read.is_supplementary:
                continue
            if read.get_overlap(junc_site - THRESHOLD, junc_site + THRESHOLD) >= THRESHOLD * 2:
                cand_reads.append((read.query_name, read.is_read1 - read.is_read2, circ_id))
    sam.close()
    return cand_reads


def proc_genome_bam(bam_file, thread, circ_info, cand_reads, threshold):
    """
    Extract FSJ reads and check BSJ reads alignment information

    Returns
    -----
    dict
        bsj reads of circRNAs, pair_id -> mate_id -> circ_id
    dict
        fsj reads of circRNAs, pair_id -> mate_id -> circ_id

    """
    LOGGER.info('Detecting FSJ reads from genome alignment file')

    sam = pysam.AlignmentFile(bam_file, 'rb')
    header = sam.header['SQ']
    sam.close()

    pool = Pool(thread, genome_initializer, (bam_file, circ_info, cand_reads, threshold))
    jobs = []
    for chrom_info in header:
        jobs.append(pool.apply_async(genome_worker, (chrom_info['SN'], )))
    pool.close()
    pool.join()

    fp_bsj = defaultdict(dict)
    fsj_reads = defaultdict(dict)

    for job in jobs:
        chrom_fp_bsj, chrom_fsj = job.get()
        for pair_id, mate_id in chrom_fp_bsj:
            fp_bsj[pair_id][mate_id] = 1
        for pair_id, mate_id, circ_id in chrom_fsj:
            fsj_reads[pair_id][mate_id] = circ_id

    circ_bsj = defaultdict(dict)
    circ_fsj = defaultdict(dict)
    for pair_id in cand_reads:
        for mate_id, circ_id in cand_reads[pair_id].iteritems():
            if pair_id in fp_bsj and mate_id in fp_bsj[pair_id]:
                continue
            circ_bsj[circ_id].update({query_prefix(pair_id): 1})

    for pair_id in fsj_reads:
        for mate_id, circ_id in fsj_reads[pair_id].iteritems():
            if pair_id in cand_reads and mate_id in cand_reads[pair_id] and not (pair_id in fp_bsj and mate_id in fp_bsj[pair_id]):
                continue
            circ_fsj[circ_id].update({query_prefix(pair_id): 1})

    return circ_bsj, circ_fsj


CIRC = None
BSJ = None
def genome_initializer(bam_file, circ_info, cand_bsj, threshold):
    """
    Initializer for passing bam file name and circRNA_info
    """
    global BAM, CIRC, THRESHOLD, BSJ
    BAM, CIRC, BSJ, THRESHOLD = bam_file, circ_info, cand_bsj, threshold


def genome_worker(chrom):
    """
    Find FSJ reads and re-check BSJ reads

    Parameters
    -----
    chrom : str
        chromosme or scaffold name for process

    Returns
    -----
    list
        false positive reads information,  (query_name, mate_id)
    list
        fsj_reads of circRNAs, (query_name, mate_id, circ_id)

    """

    if chrom not in CIRC:
        return {}, {}

    sam = pysam.AlignmentFile(BAM, 'rb')

    fp_bsj = []
    for read in sam.fetch(chrom, multiple_iterators=True):
        # If Reads is bsj candidate
        if read.is_unmapped:
            continue
        if read.query_name not in BSJ:
            continue
        if read.is_read1 - read.is_read2 not in BSJ[read.query_name]:
            continue
        circ_id = BSJ[read.query_name][read.is_read1 - read.is_read2]
        # check alignment against refernce genome
        if is_linear(read.cigartuples[0]) and is_linear(read.cigartuples[-1]):
            fp_bsj.append((read.query_name, read.is_read1 - read.is_read2))

    fsj_reads = []
    for circ_id, parser in CIRC[chrom].iteritems():
        # FSJ across start site
        for read in sam.fetch(region='{0}:{1}-{1}'.format(chrom, parser.start)):
            if read.is_unmapped or read.is_supplementary:
                continue
            if not read.get_overlap(parser.start - 1, parser.start + THRESHOLD - 1) == THRESHOLD:
                continue
            if is_mapped(read.cigartuples[0]) and is_mapped(read.cigartuples[-1]):
                fsj_reads.append((read.query_name, read.is_read1 - read.is_read2, circ_id))

        for read in sam.fetch(region='{0}:{1}-{1}'.format(chrom, parser.end)):
            if read.is_unmapped or read.is_supplementary:
                continue
            if not read.get_overlap(parser.end - THRESHOLD, parser.end) == THRESHOLD:
                continue
            if is_mapped(read.cigartuples[0]) and is_mapped(read.cigartuples[-1]):
                fsj_reads.append((read.query_name, read.is_read1 - read.is_read2, circ_id))

    sam.close()

    return fp_bsj, fsj_reads


def is_mapped(cigar_tuple):
    """
    Whether end of alignment segment is a mapped end

    Parameters
    -----
    cigar_tuple : tuple of cigar

    Returns
    -----
    int
        1 for linear end, 0 for ambiguous end

    """
    if cigar_tuple[0] == 0 or cigar_tuple[1] <= 10:
        return 1
    else:
        return 0


def is_linear(cigar_tuple):
    """
    Whether end of alignment segment is a linear end

    Parameters
    -----
    cigar_tuple : tuple of cigar

    Returns
    -----
    int
        1 for linear end, 0 for ambiguous end

    """
    if cigar_tuple[0] == 0 and cigar_tuple[1] >= 5:
        return 1
    else:
        return 0


def query_prefix(query_name):
    """
    Get pair id without mate id marker

    Paramters
    -----
    read : pysam.AlignedSegment

    Returns
    -----
    str
        mate id of segment

    """
    prefix_m = PREFIX.search(query_name)
    prefix = prefix_m.group(1) if prefix_m else query_name
    return prefix


def proc(log_file, thread, circ_file, hisat_bam, reads, outdir, prefix, anchor, config):
    """
    Build pseudo circular reference index and perform reads re-alignment
    Extract BSJ and FSJ reads from alignment results

    Returns
    -----
    str
        output file name

    """
    from utils import check_dir
    circ_dir = '{}/circ'.format(outdir)
    check_dir(circ_dir)

    circ_fasta = '{}/circ/{}_index.fa'.format(outdir, prefix)
    circ_info = load_bed(circ_file)

    # extract fasta file for reads alignment
    generate_index(log_file, circ_info, config, circ_fasta)

    # hisat2-build index
    denovo_index = build_index(log_file, thread, circ_fasta, outdir, prefix, config)
    LOGGER.debug('De-novo index: {}'.format(denovo_index))

    # hisat2 de novo alignment for candidate reads
    denovo_bam = denovo_alignment(log_file, thread, reads, outdir, prefix, config)
    LOGGER.debug('De-novo bam: {}'.format(denovo_bam))

    # Find BSJ and FSJ informations
    cand_bsj = proc_denovo_bam(denovo_bam, thread, anchor)
    bsj_reads, fsj_reads = proc_genome_bam(hisat_bam, thread, circ_info, cand_bsj, anchor)
    circ_list = bsj_reads.keys()

    total_reads, mapped_reads = bam_stat(hisat_bam)
    circ_reads = sum([len(bsj_reads[i]) for i in bsj_reads]) * 2

    # circRNA annotation
    gtf_info = index_annotation(config['gtf'])

    stat_file = '{}/{}.stat'.format(outdir, prefix)
    with open(stat_file, 'w') as out:
        json.dump({'Total_Reads': total_reads, 'Mapped_Reads': mapped_reads, 'Circ_Reads': circ_reads}, out)

    out_file = '{}/{}.gtf'.format(outdir, prefix)
    format_output(circ_info, bsj_reads, fsj_reads, gtf_info, circ_list, out_file)

    return out_file


def bam_stat(bam_file):
    """
    Stat of bam file

    Returns
    -----
    int
        number of total reads
    int
        number of mapped reads

    """
    sam = pysam.AlignmentFile(bam_file, 'rb')
    total = sam.count(read_callback=total_callback, until_eof=True)
    unmapped = sam.count(read_callback=unmapped_callback)
    return total, total - unmapped


def unmapped_callback(read):
    """
    callback for counting unmapped reads
    """
    return read.is_unmapped or read.mate_is_unmapped and not read.is_supplementary and not read.is_secondary


def total_callback(read):
    """
    callback for counting total reads
    """
    return not read.is_supplementary and not read.is_secondary


def format_output(circ_info, bsj_reads, fsj_reads, gtf_index, circ_list, outfile):
    """
    Output bsj information of circRNA expression levels

    Parameters
    -----
    circ_info : dict
        all circRNA informations, chrom -> circ_id -> BedParser
    bsj_reads : dict
        dict of bsj reads of circRNAs, circ_id -> query_name -> 1
    fsj_reads : dict
        dict of fsj reads of circRNAs, circ_id -> query_name -> 1
    outfile : str
        output file name

    """
    LOGGER.info('Output circRNA expression values')

    with open(outfile, 'w') as out:
        for chrom in sorted(circ_info.keys(), key=by_chrom):
            for circ_id in sorted(circ_info[chrom].keys(), cmp=by_circ, key=lambda x:circ_info[chrom][x]):
                if circ_id not in circ_list:
                    continue
                parser = circ_info[chrom][circ_id]
                tmp_line = [chrom, 'CIRIquant', circ_id, parser.start, parser.end, ]
                bsj = len(bsj_reads[circ_id]) if circ_id in bsj_reads else 0
                fsj = len(fsj_reads[circ_id]) if circ_id in fsj_reads else 0
                # Junction ratio
                try:
                    junc = 2.0 * bsj / (2.0 * bsj + fsj)
                except Exception as e:
                    junc = 0.0

                strand = parser.strand
                tmp_line = [
                    chrom,
                    'circRNA',
                    circ_id,
                    parser.start,
                    parser.end,
                    bsj,
                    strand,
                    '.',
                ]

                field = circRNA_attr(gtf_index, parser)
                tmp_attr = 'bsj {:.1f}; fsj {:.1f}; junc_ratio {:.3f};'.format(bsj, fsj, junc)
                for key in 'circ_type', 'gene_id', 'gene_name', 'gene_type':
                    if key in field:
                        tmp_attr += ' {} "{}";'.format(key, field[key])
                tmp_line.append(tmp_attr)

                out.write('\t'.join([str(x) for x in tmp_line]) + '\n')
    return 1


def by_chrom(x):
    """
    Sort by chromosomes
    """
    chrom = x
    if x.startswith('chr'):
        chrom = chrom.strip('chr')
    try:
        chrom = int(chrom)
    except Exception as e:
        pass
    return chrom


def by_circ(x, y):
    """
    Sort circRNAs by the start and end position
    """
    return x.end - y.end if x.start == y.start else x.start - y.start


class GeneParser(object):
    """
    Class for parsing annotation gtf
    """

    def __init__(self, content):
        self.chrom = content[0]
        self.source = content[1]
        self.type = content[2]
        self.start, self.end = int(content[3]), int(content[4])
        self.strand = content[6]
        self.attr_string = content[8]


    @property
    def attr(self):
        """
        Parsing attribute column in gtf file
        """
        field = {}
        for key, value in [re.split('\s+', i.strip()) for i in self.attr_string.split(';') if i != '']:
            field[key] = value.strip('"')
        return field


def index_annotation(gtf):
    """
    Generate binned index for element in gtf
    """

    LOGGER.info('Loading annotation gtf ..')
    gtf_index = defaultdict(dict)
    with open(gtf, 'r') as f:
        for line in f:
            if line.startswith('#'):
                continue
            content = line.rstrip().split('\t')
            # only include gene and exon feature for now
            if content[2] not in ['gene', 'exon']:
                continue
            parser = GeneParser(content)
            start_div, end_div = parser.start / 500, parser.end / 500
            for i in xrange(start_div, end_div + 1):
                gtf_index[parser.chrom].setdefault(i, []).append(parser)
    return gtf_index


def circRNA_attr(gtf_index, circ):
    """
    annotate circRNA information
    """
    if circ.chr not in gtf_index:
        sys.exit('chrom of contig "{}" not in annotation gtf, please check'.format(circ.chrom))
    start_div, end_div = circ.start / 500, circ.end / 500

    host_gene = {}
    start_element = defaultdict(list)
    end_element = defaultdict(list)

    single_gene = 1
    for x in xrange(start_div, end_div + 1):
        if x not in gtf_index[circ.chr]:
            single_gene = 0
            continue
        for element in gtf_index[circ.chr][x]:
            # start site
            if element.start <= circ.start <= element.end and element.strand == circ.strand:
                start_element[element.type].append(element)
            # end site
            if element.start <= circ.end <= element.end and element.strand == circ.strand:
                end_element[element.type].append(element)
            # annotation
            if element.type != 'gene':
                continue
            if element.end < circ.start or circ.end < element.start:
                continue
            if element.attr['gene_id'] not in host_gene:
                host_gene[element.attr['gene_id']] = element

    circ_type = {}
    forward_host_gene = []

    if host_gene and single_gene:
        for gene_id in host_gene:
            if host_gene[gene_id].strand == circ.strand:
                forward_host_gene.append(host_gene[gene_id])
                if 'gene' in start_element and 'gene' in end_element:
                    if 'exon' in start_element and 'exon' in end_element:
                        circ_type['exon'] = 1
                    else:
                        circ_type['intron'] = 1
                else:
                    circ_type['gene_intergenic'] = 1
            else:
                circ_type['antisense'] = 1
    else:
        circ_type['intergenic'] = 1

    field = {}
    if 'exon' in circ_type:
        field['circ_type'] = 'exon'
    elif 'intron' in circ_type:
        field['circ_type'] = 'intron'
    elif 'gene_intergenic' in circ_type:
        field['circ_type'] = 'intergenic'
    elif 'antisense' in circ_type:
        field['circ_type'] = 'antisense'
    else:
        field['circ_type'] = 'intergenic'

    # gene_id
    # gene_name
    # gene_type / gene_biotype

    if len(forward_host_gene) == 1:
        field.update({
            'gene_id': forward_host_gene[0].attr['gene_id'],
            'gene_name': forward_host_gene[0].attr['gene_name'],
            'gene_type': forward_host_gene[0].attr['gene_type'] if 'gene_type' in forward_host_gene[0].attr else forward_host_gene[0].attr['gene_biotype'],
        })
    else:
        tmp_gene_id = []
        tmp_gene_name = []
        tmp_gene_type = []
        for x in forward_host_gene:
            tmp_gene_id.append(x.attr['gene_id'])
            tmp_gene_name.append(x.attr['gene_name'])
            tmp_gene_type.append(x.attr['gene_type'] if 'gene_type' in x.attr else x.attr['gene_biotype'])
        field.update({
            'gene_id': ','.join(tmp_gene_id),
            'gene_name': ','.join(tmp_gene_name),
            'gene_type': ','.join(tmp_gene_type),
        })
    return field