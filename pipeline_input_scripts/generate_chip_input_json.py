import argparse
import json
import os
import pandas as pd
import requests


def get_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('-i', "--infile", action='store',
                        help="""Path to .txt file containing accessions of experiments to process. The txt file must contain two columns with 1 header row, one labeled 'accession' and another labeled 'align_only'. It can optionally include 'custom_message' and 'custom_crop_length' columns.""")
    parser.add_argument('-o', '--outputpath', action='store', default='',
                        help="""Optional path to output folder. Defaults to current path.""")
    parser.add_argument('-g', '--gcpath', action='store', default='',
                        help="""Optional path where the input.json will be uploaded to the Google Cloud instance. Only affects the list of caper commands that is generated.""")
    parser.add_argument('--wdl', action='store', default=False,
                        help="""Path to .wdl file.""")
    parser.add_argument('-s', '--server', action='store', default='https://www.encodeproject.org',
                        help="""Optional specification of server using the full URL. Defaults to production server.""")
    parser.add_argument('--use-s3-uris', action='store_true', default=False,
                        help="""Optional flag to use s3_uri links. Otherwise, defaults to using @@download links from the ENCODE portal.""")
    input_group.add_argument("--accessions", action='store',
                        help="""List of accessions separated by commas.""")
    parser.add_argument('--align-only', action='store', default=False,
                        help="""Pipeline will end after alignments step if True.""")
    parser.add_argument('--custom-message', action='store',
                        help="""An additional custom string to be appended to the messages in the caper submit commands.""")
    parser.add_argument('--caper-commands-file-message', action='store', default='',
                        help="""An additional custom string to be appended to the file name of the caper submit commands.""")
    parser.add_argument('--custom-crop-length', action='store', default='',
                        help="""Custom value for the crop length.""")
    parser.add_argument('--multiple-controls', action='store', default='',
                        help="""Pipeline will assume multiple controls should be used.""")
    parser.add_argument('--force-se', action='store', default='',
                        help="""Pipeline will map as single-ended regardless of input fastqs.""")
    parser.add_argument('--redacted', action='store', default='',
                        help="""Control experiment has redacted alignments.""")
    return parser


def check_path_trailing_slash(path):
    if path.endswith('/'):
        return path.rstrip('/')
    else:
        return path


def build_experiment_report_query(experiment_list, server):
    joined_list = '&accession='.join(experiment_list)
    return server + '/report/?type=Experiment' + \
        f'&accession={joined_list}' + \
        '&field=@id' + \
        '&field=accession' + \
        '&field=assay_title' + \
        '&field=control_type' + \
        '&field=possible_controls' + \
        '&field=replicates.antibody.targets' + \
        '&field=files.s3_uri' + \
        '&field=files.href' + \
        '&field=replicates.library.biosample.organism.scientific_name' + \
        '&limit=all' + \
        '&format=json'


def build_file_report_query(experiment_list, server, file_format):
    joined_list = '&dataset='.join(experiment_list)
    if file_format == 'fastq':
        format_parameter = '&file_format=fastq'
        award_parameter = ''
        output_type_parameter = '&output_type=reads'
    elif file_format == 'bam':
        format_parameter = '&file_format=bam'
        award_parameter = '&award.rfa=ENCODE4'
        output_type_parameter = '&output_type=alignments&output_type=redacted alignments'
    return server + '/report/?type=File' + \
        f'&dataset={joined_list}' + \
        '&status=released' + \
        '&status=in+progress' + \
        award_parameter + \
        '&assembly!=hg19' + \
        '&assembly!=mm9' + \
        format_parameter + \
        output_type_parameter + \
        '&field=@id' + \
        '&field=dataset' + \
        '&field=file_format' + \
        '&field=biological_replicates' + \
        '&field=paired_end' + \
        '&field=paired_with' + \
        '&field=run_type' + \
        '&field=mapped_run_type' + \
        '&field=read_length' + \
        '&field=cropped_read_length' + \
        '&field=cropped_read_length_tolerance' + \
        '&field=status' + \
        '&field=s3_uri' + \
        '&field=href' + \
        '&field=replicate.status' + \
        '&limit=all' + \
        '&format=json'


def parse_infile(infile):
    try:
        infile_df = pd.read_csv(infile, sep='\t')
        infile_df['align_only'].astype('bool')
        infile_df['multiple_controls'].astype('bool')
        infile_df['force_se'].astype('bool')
        return infile_df
    except FileNotFoundError as e:
        print(e)
        exit()
    except KeyError:
        print('Missing required align_only column in input file.')
        exit()


def strs2bool(strings):
    out = []
    for string in strings:
        if string == "True":
            out.append(True)
        elif string == "False":
            out.append(False)
    return out


def get_data_from_portal(infile_df, server, keypair, link_prefix, link_src):
    # Retrieve experiment report view json with necessary fields and store as DataFrame.
    experiment_input_df = pd.DataFrame()
    experiment_accessions = infile_df['accession'].tolist()
    # Chunk the list to avoid sending queries longer than the character limit
    chunked_experiment_accessions = [experiment_accessions[x:x+100] for x in range(0, len(experiment_accessions), 100)]
    for chunk in chunked_experiment_accessions:
        experiment_report = requests.get(
            build_experiment_report_query(chunk, server),
            auth=keypair,
            headers={'content-type': 'application/json'})
        experiment_report_json = json.loads(experiment_report.text)
        experiment_df_temp = pd.json_normalize(experiment_report_json['@graph'])
        experiment_input_df = pd.concat([experiment_input_df, experiment_df_temp], ignore_index=True, sort=True)
    experiment_input_df.sort_values(by=['accession'], inplace=True)

    # Fill in columns that may be missing
    if 'control_type' not in experiment_input_df:
        experiment_input_df['control_type'] = None

    # Retrieve list of wildtype controls
    wildtype_ctl_query_res = requests.get(
        link_prefix+'/search/?type=Experiment&assay_title=Control+ChIP-seq&replicates.library.biosample.applied_modifications%21=%2A&limit=all',
        auth=keypair,
        headers={'content-type': 'application/json'})
    wildtype_ctl_ids = [ctl['@id'] for ctl in json.loads(wildtype_ctl_query_res.text)['@graph']]

    # Gather list of controls from the list of experiments to query for their files.
    datasets_to_retrieve = experiment_input_df.get('@id').tolist()
    for ctl in experiment_input_df.get('possible_controls'):
        for item in ctl:
            datasets_to_retrieve.append(item['@id'])

    # Retrieve file report view json with necessary fields and store as DataFrame.
    file_input_df = pd.DataFrame()
    chunked_dataset_accessions = [datasets_to_retrieve[x:x+100] for x in range(0, len(datasets_to_retrieve), 100)]
    for chunk in chunked_dataset_accessions:
        for file_format in ['fastq', 'bam']:
            file_report = requests.get(
                build_file_report_query(chunk, server, file_format),
                auth=keypair,
                headers={'content-type': 'application/json'})
            file_report_json = json.loads(file_report.text)
            file_df_temp = pd.json_normalize(file_report_json['@graph'])
            file_input_df = pd.concat([file_input_df, file_df_temp], ignore_index=True, sort=True)
    file_input_df.set_index(link_src, inplace=True)
    file_df_required_fields = ['paired_end', 'paired_with', 'mapped_run_type']
    for field in file_df_required_fields:
        if field not in file_input_df:
            file_input_df[field] = None
    file_input_df['biorep_scalar'] = [x[0] for x in file_input_df['biological_replicates']]

    return experiment_input_df, wildtype_ctl_ids, file_input_df


# Simple function to count the number of replicates per input.json
def count_reps(row):
    x = 0
    for value in row:
        if None in value or value == []:
            continue
        else:
            x = x+1
    return x


def main():
    keypair = (os.environ.get('DCC_API_KEY'), os.environ.get('DCC_SECRET_KEY'))
    parser = get_parser()
    args = parser.parse_args()
    allowed_statuses = ['released', 'in progress']

    output_path = check_path_trailing_slash(args.outputpath)
    wdl_path = args.wdl
    gc_path = args.gcpath
    caper_commands_file_message = args.caper_commands_file_message

    server = check_path_trailing_slash(args.server)
    use_s3 = args.use_s3_uris
    if use_s3:
        link_prefix = ''
        link_src = 's3_uri'
    else:
        link_prefix = server
        link_src = 'href'

    if args.infile:
        infile_df = parse_infile(args.infile)
        infile_df.sort_values(by=['accession'], inplace=True)
        infile_df.drop_duplicates(subset=['accession'],inplace=True)
    elif args.accessions:
        accession_list = args.accessions.split(',')
        align_only = strs2bool(args.align_only.split(','))
        message = args.custom_message.split(',')
        custom_crop_length = args.custom_crop_length.split(',')
        multiple_controls = strs2bool(args.multiple_controls.split(','))
        force_se = strs2bool(args.force_se.split(','))
        redacted = strs2bool(args.redacted.split(','))
        infile_df = pd.DataFrame({
            'accession': accession_list,
            'align_only': align_only,
            'custom_message': message,
            'crop_length': custom_crop_length,
            'multiple_controls': multiple_controls,
            'force_se': force_se,
            'redacted': redacted
        })
        infile_df.sort_values(by=['accession'], inplace=True)

    use_custom_crop_length_flag = False
    if 'custom_crop_length' in infile_df:
        use_custom_crop_length_flag = True
        custom_crop_lengths = infile_df['custom_crop_length'].tolist()
    else:
        custom_crop_lengths = [None] * len(infile_df['accession'])

    force_se_flag = False
    if 'force_se' in infile_df:
        force_se_flag = True
        force_ses = infile_df['force_se'].tolist()
    else:
        force_ses = False * len(infile_df['accession'])

    if 'redacted' in infile_df:
        redacted_flags = [x if x is True else None for x in infile_df['redacted'].tolist()]
    else:
        redacted_flags = [None] * len(infile_df['accession'])

    if 'multiple_controls' in infile_df:
        multiple_controls = infile_df['multiple_controls'].tolist()
    else:
        multiple_controls = False * len(infile_df['accession'])

    # Arrays to store lists of potential errors.
    ERROR_no_fastqs = []
    ERROR_missing_fastq_pairs = []
    ERROR_control_error_detected = []
    ERROR_not_matching_endedness = []

    # Fetch data from the ENCODE portal
    experiment_input_df, wildtype_ctl_ids, file_input_df = get_data_from_portal(infile_df, server, keypair, link_prefix, link_src)

    # Create output_df to store all data for the final input.json files.
    output_df = pd.DataFrame()
    output_df['chip.title'] = infile_df['accession']
    output_df['chip.align_only'] = infile_df['align_only']
    if 'custom_message' in infile_df:
        output_df['custom_message'] = infile_df['custom_message']
        output_df['custom_message'].fillna('', inplace=True)
    else:
        output_df['custom_message'] = ''
    output_df.set_index('chip.title', inplace=True, drop=False)
    output_df['assay_title'] = experiment_input_df['assay_title'].to_list()

    '''
    Experiment sorting section
    '''

    # Assign blacklist(s) and genome reference file.
    blacklist = []
    blacklist2 = []
    genome_tsv = []
    chrom_sizes = []
    ref_fa = []
    bowtie2 = []
    # Only (human) Mint-ChIP-seq should have bwa_idx_tar value.
    bwa_index = []
    for assay, replicates in zip(experiment_input_df.get('assay_title'), experiment_input_df.get('replicates')):
        organism = set()
        for rep in replicates:
            organism.add(rep['library']['biosample']['organism']['scientific_name'])

        if ''.join(organism) == 'Homo sapiens':
            genome_tsv.append('https://storage.googleapis.com/encode-pipeline-genome-data/genome_tsv/v3/hg38.tsv')
            chrom_sizes.append('https://www.encodeproject.org/files/GRCh38_EBV.chrom.sizes/@@download/GRCh38_EBV.chrom.sizes.tsv')
            ref_fa.append('https://www.encodeproject.org/files/GRCh38_no_alt_analysis_set_GCA_000001405.15/@@download/GRCh38_no_alt_analysis_set_GCA_000001405.15.fasta.gz')
            if assay in ['Mint-ChIP-seq', 'Control Mint-ChIP-seq']:
                blacklist.append('https://www.encodeproject.org/files/ENCFF356LFX/@@download/ENCFF356LFX.bed.gz')
                blacklist2.append('https://www.encodeproject.org/files/ENCFF023CZC/@@download/ENCFF023CZC.bed.gz')
                bowtie2.append(None)
                bwa_index.append('https://www.encodeproject.org/files/ENCFF643CGH/@@download/ENCFF643CGH.tar.gz')
            elif assay in ['Histone ChIP-seq', 'TF ChIP-seq', 'Control ChIP-seq']:
                blacklist.append('https://www.encodeproject.org/files/ENCFF356LFX/@@download/ENCFF356LFX.bed.gz')
                blacklist2.append(None)
                bowtie2.append('https://www.encodeproject.org/files/ENCFF110MCL/@@download/ENCFF110MCL.tar.gz')
                bwa_index.append(None)
        elif ''.join(organism) == 'Mus musculus':
            genome_tsv.append('https://storage.googleapis.com/encode-pipeline-genome-data/genome_tsv/v3/mm10.tsv')
            chrom_sizes.append('https://www.encodeproject.org/files/mm10_no_alt.chrom.sizes/@@download/mm10_no_alt.chrom.sizes.tsv')
            ref_fa.append('https://www.encodeproject.org/files/mm10_no_alt_analysis_set_ENCODE/@@download/mm10_no_alt_analysis_set_ENCODE.fasta.gz')
            if assay in ['Mint-ChIP-seq', 'Control Mint-ChIP-seq']:
                blacklist.append(None)
                blacklist2.append(None)
                bowtie2.append(None)
                bwa_index.append(None)
            elif assay in ['Histone ChIP-seq', 'TF ChIP-seq', 'Control ChIP-seq']:
                blacklist.append('https://www.encodeproject.org/files/ENCFF547MET/@@download/ENCFF547MET.bed.gz')
                blacklist2.append(None)
                bowtie2.append('https://www.encodeproject.org/files/ENCFF309GLL/@@download/ENCFF309GLL.tar.gz')
                bwa_index.append(None)
    output_df['chip.blacklist'] = blacklist
    output_df['chip.blacklist2'] = blacklist2
    output_df['chip.genome_tsv'] = genome_tsv
    output_df['chip.chrsz'] = chrom_sizes
    output_df['chip.ref_fa'] = ref_fa
    output_df['chip.bowtie2_idx_tar'] = bowtie2
    output_df['chip.bwa_idx_tar'] = bwa_index

    # Determine pipeline types and bwa related properties for Mint
    pipeline_types = []
    aligners = []
    use_bwa_mem_for_pes = []
    bwa_mem_read_len_limits = []
    for assay, ctl_type in zip(experiment_input_df.get('assay_title'), experiment_input_df.get('control_type')):
        if pd.notna(ctl_type) and assay == 'Control ChIP-seq':
            pipeline_types.append('control')
            aligners.append('')
            use_bwa_mem_for_pes.append('')
            bwa_mem_read_len_limits.append('')
        elif pd.notna(ctl_type) and assay == 'Control Mint-ChIP-seq':
            pipeline_types.append('control')
            aligners.append('bwa')
            use_bwa_mem_for_pes.append(True)
            bwa_mem_read_len_limits.append(0)
        elif assay == 'TF ChIP-seq':
            pipeline_types.append('tf')
            aligners.append('')
            use_bwa_mem_for_pes.append('')
            bwa_mem_read_len_limits.append('')
        elif assay == 'Histone ChIP-seq':
            pipeline_types.append('histone')
            aligners.append('')
            use_bwa_mem_for_pes.append('')
            bwa_mem_read_len_limits.append('')
        elif assay == 'Mint-ChIP-seq':
            pipeline_types.append('histone')
            aligners.append('bwa')
            use_bwa_mem_for_pes.append(True)
            bwa_mem_read_len_limits.append(0)

    # Arrays which will be added to the master Dataframe for all experiments
    crop_length = []
    fastqs_by_rep_R1_master = {
        1: [], 2: [],
        3: [], 4: [],
        5: [], 6: [],
        7: [], 8: [],
        9: [], 10: []
    }
    fastqs_by_rep_R2_master = {
        1: [], 2: [],
        3: [], 4: [],
        5: [], 6: [],
        7: [], 8: [],
        9: [], 10: []
    }
    # Store experiment read lengths and run types for comparison against controls
    experiment_min_read_lengths = []
    experiment_run_types = []

    for experiment_files, experiment_id, custom_crop_length, map_as_SE in zip(
        experiment_input_df['files'],
        experiment_input_df['accession'],
        custom_crop_lengths,
        force_ses
    ):
        # Arrays for files within each experiment
        fastqs_by_rep_R1 = {
            1: [], 2: [],
            3: [], 4: [],
            5: [], 6: [],
            7: [], 8: [],
            9: [], 10: []
        }
        fastqs_by_rep_R2 = {
            1: [], 2: [],
            3: [], 4: [],
            5: [], 6: [],
            7: [], 8: [],
            9: [], 10: []
        }
        experiment_read_lengths = []
        run_types = set()

        for file in experiment_files:
            link = file[link_src]
            if link.endswith('fastq.gz') \
                    and link in file_input_df.index \
                    and file_input_df.loc[link].at['status'] in allowed_statuses \
                    and file_input_df.loc[link].at['replicate.status'] in allowed_statuses:
                if file_input_df.loc[link].at['paired_end'] == '1':
                    pair = file_input_df.loc[link].at['paired_with']
                    for rep_num in fastqs_by_rep_R1:
                        if file_input_df.loc[link].at['biorep_scalar'] == rep_num:
                            fastqs_by_rep_R1[rep_num].append(link_prefix + link)
                            if not map_as_SE:
                                try:
                                    fastqs_by_rep_R2[rep_num].append(link_prefix + file_input_df[file_input_df['@id'] == pair].index.values[0])
                                except IndexError:
                                    print(f'ERROR: Metadata error (missing expected read 2 fastq) in {experiment_id}.')
                                    ERROR_missing_fastq_pairs.append(experiment_id)
                elif pd.isnull(file_input_df.loc[link].at['paired_end']):
                    for rep_num in fastqs_by_rep_R1:
                        if file_input_df.loc[link].at['biorep_scalar'] == rep_num:
                            fastqs_by_rep_R1[rep_num].append(link_prefix + link)

                # Collect read_lengths and run_types
                experiment_read_lengths.append(file_input_df.loc[link].at['read_length'])
                run_types.add(file_input_df.loc[link].at['run_type'])

        # Record error if no fastqs for found for any replicate.
        if all(val == [] for val in fastqs_by_rep_R1.values()):
            print(f'ERROR: no fastqs were found for {experiment_id}.')
            ERROR_no_fastqs.append(experiment_id)

        # Fix ordering of reps to prevent non-consecutive numbering.
        for k in list(range(1, 11)):
            if fastqs_by_rep_R1[k] == []:
                for i in list(range(k+1, 11)):
                    if fastqs_by_rep_R1[i] != []:
                        fastqs_by_rep_R1[k] = fastqs_by_rep_R1[i]
                        fastqs_by_rep_R2[k] = fastqs_by_rep_R2[i]
                        fastqs_by_rep_R1[i] = []
                        fastqs_by_rep_R2[i] = []
                        break
                    else:
                        continue

        # Add the replicates to the master list.
        for rep_num in fastqs_by_rep_R1_master:
            fastqs_by_rep_R1_master[rep_num].append(fastqs_by_rep_R1[rep_num])
            fastqs_by_rep_R2_master[rep_num].append(fastqs_by_rep_R2[rep_num])

        if use_custom_crop_length_flag:
            experiment_min_read_lengths.append(custom_crop_length)
        else:
            experiment_min_read_lengths.append(min(experiment_read_lengths))

        if 'single-ended' in run_types:
            experiment_run_types.append('single-ended')
        elif next(iter(run_types)) == 'paired-ended':
            experiment_run_types.append('paired-ended')

    '''
    Control sorting section
    '''

    ctl_nodup_bams = []
    final_run_types = []
    for controls, experiment, pipeline_type, experiment_run_type, replicates, experiment_read_length, use_multiple_controls, map_as_SE in zip(
            experiment_input_df['possible_controls'],
            experiment_input_df['accession'],
            pipeline_types,
            experiment_run_types,
            experiment_input_df['replicates'],
            experiment_min_read_lengths,
            multiple_controls,
            force_ses
    ):
        try:
            if pipeline_type == 'control':
                ctl_nodup_bams.append(None)
                final_run_types.append(False if experiment_run_type == 'single-ended' or map_as_SE else True)
                crop_length.append(experiment_read_length)
            elif controls == []:
                print(f'ERROR: No controls in possible_controls for experiment {experiment}.')
                raise Warning
            else:
                if len(controls) > 1 and not use_multiple_controls:
                    # Only check TF ChIP if the antibody is eGFP; otherwise throw
                    # an error if there are more than one control specified.
                    antibody = set()
                    for rep in replicates:
                        if 'antibody' in rep:
                            for target in rep['antibody']['targets']:
                                antibody.add(target)
                        else:
                            print(f'ERROR: Replicate in {experiment} is missing metadata about the antibody used.')
                            raise Warning
                    if ''.join(antibody) == '/targets/eGFP-avictoria/' and pipeline_type == 'tf':
                        for ctl in controls:
                            if ctl['@id'] in wildtype_ctl_ids:
                                controls = [ctl]
                                break
                        if len(controls) == 0:
                            print(f'ERROR: Could not locate wildtype control for {experiment}.')
                            raise Warning
                    else:
                        print(f'ERROR: Too many controls for experiment {experiment}.')
                        raise Warning

                control_run_types = set()
                control_read_lengths = list()
                for control in controls:
                    # Identify run_types in the control(s)
                    control_run_types.update(file_input_df[
                            (file_input_df['dataset'] == control['@id']) &
                            (file_input_df['file_format'] == 'fastq')
                            ].get('run_type'))
                    # Collect read_lengths in the control(s)
                    control_read_lengths.extend(file_input_df[
                            (file_input_df['dataset'] == control['@id']) &
                            (file_input_df['file_format'] == 'fastq')
                            ].get('read_length').tolist())

                # Determine endedness based on the run types of the control(s) and experiment.
                if 'single-ended' in control_run_types or experiment_run_type == 'single-ended' or map_as_SE:
                    final_run_type = 'single-ended'
                    final_run_types.append(False)
                elif next(iter(control_run_types)) == 'paired-ended' and experiment_run_type == 'paired-ended':
                    final_run_type = 'paired-ended'
                    final_run_types.append(True)
                else:
                    ERROR_not_matching_endedness.append(experiment)
                    print(f'ERROR: Could not determine correct endedness for experiment {experiment} and its control.')
                    raise Warning

                # Select the minimum read length out of the files in the experiment
                # and its control, and store the value.
                combined_minimum_read_length = min([experiment_read_length] + control_read_lengths)
                if use_custom_crop_length_flag:
                    crop_length.append(experiment_read_length)
                else:
                    crop_length.append(combined_minimum_read_length)

                # Gather control bams based on matching read_length
                ctl_nodup_temp_collector = []
                for control in controls:
                    matching_bam_found = False
                    for rep_num in list(range(1, 11)):
                        ctl_search = file_input_df[
                            (file_input_df['dataset'] == control['@id']) &
                            (file_input_df['biorep_scalar'] == rep_num) &
                            (file_input_df['file_format'] == 'bam') &
                            (file_input_df['mapped_run_type'] == final_run_type) &
                            (file_input_df['cropped_read_length'] <= combined_minimum_read_length + 2) &
                            (file_input_df['cropped_read_length'] >= combined_minimum_read_length - 2)
                        ]
                        if not ctl_search.empty:
                            if ctl_search['cropped_read_length_tolerance'].values[0] == 2:
                                ctl_nodup_temp_collector.append(link_prefix + ctl_search.index.values[0])
                            else:
                                print(f'ERROR: Tolerance of control bam {ctl_search["@id"].values[0]} is not 2 bp.')
                                ctl_nodup_temp_collector.append(None)
                            matching_bam_found = True
                    # If the experiment has multiple controls that should be used,
                    # we expect each control to have at least one matching bam. Otherwise, treat it as an error.
                    if not matching_bam_found:
                        print(f'ERROR: no bams found in control of {experiment}.')
                        ERROR_control_error_detected.append(experiment)
                if not ctl_nodup_temp_collector:
                    print(f'ERROR: no bams found for {experiment}.')
                    ctl_nodup_bams.append(None)
                    ERROR_control_error_detected.append(experiment)
                elif None in ctl_nodup_temp_collector:
                    ctl_nodup_bams.append(None)
                    ERROR_control_error_detected.append(experiment)
                else:
                    ctl_nodup_bams.append(ctl_nodup_temp_collector)
        except Warning:
            ERROR_control_error_detected.append(experiment)
            ctl_nodup_bams.append(None)
            final_run_types.append(None)
            crop_length.append(None)

    '''
    Assign all remaining missing properties in the master dataframe.
    '''
    output_df['chip.paired_end'] = final_run_types
    output_df['chip.crop_length'] = [int(x) if x is not None else '' for x in crop_length]
    output_df['chip.ctl_nodup_bams'] = ctl_nodup_bams
    output_df['chip.aligner'] = aligners
    output_df['chip.use_bwa_mem_for_pe'] = use_bwa_mem_for_pes
    output_df['chip.bwa_mem_read_len_limit'] = bwa_mem_read_len_limits
    output_df['chip.pipeline_type'] = pipeline_types
    output_df['chip.always_use_pooled_ctl'] = [True if x != 'control' else None for x in output_df['chip.pipeline_type']]
    output_df['chip.redact_nodup_bam'] = redacted_flags

    # Populate the lists of fastqs.
    for val in list(range(1, 11)):
        output_df[f'chip.fastqs_rep{val}_R1'] = fastqs_by_rep_R1_master[val]
        output_df[f'chip.fastqs_rep{val}_R2'] = fastqs_by_rep_R2_master[val]
    R1_cols = [col for col in output_df.columns if col.endswith('_R1')]
    output_df['number_of_replicates'] = output_df[R1_cols].apply(lambda x: count_reps(x), axis=1)

    # Build descriptions using the other parameters.
    description_strings = []
    for accession, crop_length, is_paired_end, pipeline_type, align_only, num_reps, assay in zip(
            output_df['chip.title'],
            output_df['chip.crop_length'],
            output_df['chip.paired_end'],
            output_df['chip.pipeline_type'],
            output_df['chip.align_only'],
            output_df['number_of_replicates'],
            output_df['assay_title']
    ):
        description_strings.append('{}_{}_{}_{}rep_{}_{}'.format(
            accession,
            ('PE' if is_paired_end else 'SE'),
            (f'{crop_length}_crop' if 'Mint' not in assay else 'no_crop'),
            num_reps,
            pipeline_type,
            ('alignonly' if align_only else 'peakcall')
            ))
    output_df['chip.description'] = description_strings

    # Clean up the pipeline_type data - flag cases where controls are not 'align_only', then submit all 'controls' as 'tf'
    ERROR_controls_not_align_only = output_df[
        (output_df['chip.pipeline_type'] == 'control') &
        (output_df['chip.align_only'] == False)].get('chip.title').tolist()
    for expt in ERROR_controls_not_align_only:
        print(f'ERROR: {expt} is a control but was not align_only.')

    # Assign parameters that are identical for all runs.
    output_df['chip.crop_length_tol'] = 2

    # Remove any experiments with errors from the table.
    output_df.drop(
        ERROR_control_error_detected +
        ERROR_no_fastqs +
        ERROR_missing_fastq_pairs +
        ERROR_not_matching_endedness +
        ERROR_controls_not_align_only,
        inplace=True)

    # Output rows of dataframes as input json files.
    output_dict = output_df.to_dict('index')
    command_output = ''
    # Order for parameters in the input.jsons
    desired_key_order = [
        'custom_message',
        'assay_title',
        'chip.title',
        'chip.description',
        'chip.pipeline_type',
        'chip.align_only',
        'chip.paired_end',
        'chip.crop_length',
        'chip.crop_length_tol',
        'chip.genome_tsv',
        'chip.ref_fa',
        'chip.bowtie2_idx_tar',
        'chip.bwa_idx_tar',
        'chip.chrsz',
        'chip.blacklist',
        'chip.blacklist2',
        'chip.ctl_nodup_bams',
        'chip.redact_nodup_bam',
        'chip.always_use_pooled_ctl',
        'chip.aligner',
        'chip.use_bwa_mem_for_pe',
        'chip.bwa_mem_read_len_limit'
    ]
    for val in list(range(1, 11)):
        desired_key_order.extend([f'chip.fastqs_rep{val}_R1', f'chip.fastqs_rep{val}_R2'])

    for experiment in output_dict:
        output_dict[experiment] = {key: output_dict[experiment][key] for key in desired_key_order}
        # Build strings of caper commands.
        command_output = command_output + 'caper submit {} -i {}{} -s {}{}\nsleep 1\n'.format(
            wdl_path,
            (gc_path + '/' if not gc_path.endswith('/') else gc_path),
            output_dict[experiment]['chip.description'] + '.json',
            output_dict[experiment]['chip.description'],
            ('_' + output_dict[experiment]['custom_message'] if output_dict[experiment]['custom_message'] != '' else ''))

        # Remove empty properties and the custom message property.
        # All "read 2" properties should be removed if the experiment will be run as single-ended.
        if output_dict[experiment]['chip.paired_end'] is False:
            for prop in [x for x in list(output_dict[experiment]) if x.endswith('_R2')]:
                output_dict[experiment].pop(prop)
        for prop in list(output_dict[experiment]):
            if output_dict[experiment][prop] in (None, [], '') or (type(output_dict[experiment][prop]) == list and None in output_dict[experiment][prop]):
                output_dict[experiment].pop(prop)
        # Drop crop_length and crop_length_tol for Mint-ChIP only.
        if output_dict[experiment]['assay_title'] in ['Mint-ChIP-seq', 'Control Mint-ChIP-seq']:
            output_dict[experiment].pop('chip.crop_length')
            output_dict[experiment].pop('chip.crop_length_tol')
        output_dict[experiment].pop('custom_message')
        output_dict[experiment].pop('assay_title')

        file_name = f'{output_path}{"/" if output_path else ""}{output_dict[experiment]["chip.description"]}.json'
        with open(file_name, 'w') as output_file:
            output_file.write(json.dumps(output_dict[experiment], indent=4))

    # Output .txt with caper commands.
    if command_output != '':
        with open(f'{output_path}{"/" if output_path else ""}caper_submit{"_" if caper_commands_file_message else ""}{caper_commands_file_message}.sh', 'w') as command_output_file:
            command_output_file.write(command_output)


if __name__ == '__main__':
    main()
